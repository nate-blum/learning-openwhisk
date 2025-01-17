/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.openwhisk.core.containerpool

import akka.actor.{Actor, ActorRef, ActorRefFactory, Props}
import org.apache.openwhisk.common.{ContainerListKey, Logging, LoggingMarkers, MetricEmitter, TransactionId}
import org.apache.openwhisk.core.connector.MessageFeed
import org.apache.openwhisk.core.containerpool.ContainerPool.someContainerMatchesAction
import org.apache.openwhisk.core.entity.ExecManifest.ReactivePrewarmingConfig
import org.apache.openwhisk.core.entity._
import org.apache.openwhisk.core.entity.size._
import org.apache.openwhisk.core.invoker.grpc.{DeleteContainerWithIdEvent, GetBufferedInvocationsEvent, ResetInvokerEvent, SetAllowOpenWhiskToFreeMemoryEvent}
import org.apache.openwhisk.grpc.{BufferedInvocationsPerFunction, GetBufferedInvocationsResponse}

import scala.annotation.tailrec
import scala.collection.{immutable, mutable}
import scala.concurrent.duration._
import scala.util.{Random, Try}

case class ColdStartKey(kind: String, memory: ByteSize)

case object EmitMetrics

case object AdjustPrewarmedContainer
case class DeleteRandomContainer(action: ExecutableWhiskAction)

/**
 * A pool managing containers to run actions on.
 *
 * This pool fulfills the other half of the ContainerProxy contract. Only
 * one job (either Start or Run) is sent to a child-actor at any given
 * time. The pool then waits for a response of that container, indicating
 * the container is done with the job. Only then will the pool send another
 * request to that container.
 *
 * Upon actor creation, the pool will start to prewarm containers according
 * to the provided prewarmConfig, iff set. Those containers will **not** be
 * part of the poolsize calculation, which is capped by the poolSize parameter.
 * Prewarm containers are only used, if they have matching arguments
 * (kind, memory) and there is space in the pool.
 *
 * @param childFactory method to create new container proxy actor
 * @param feed actor to request more work from
 * @param prewarmConfig optional settings for container prewarming
 * @param poolConfig config for the ContainerPool
 */
class ContainerPool(childFactory: ActorRefFactory => ActorRef,
                    feed: ActorRef,
                    prewarmConfig: List[PrewarmingConfig] = List.empty,
                    poolConfig: ContainerPoolConfig)(implicit val logging: Logging)
    extends Actor {
  import ContainerPool.memoryConsumptionOf

  implicit val ec = context.dispatcher

  var allowOpenWhiskToFreeMemory = false

  var freePool = immutable.Map.empty[ActorRef, ContainerData]
  var busyPool = immutable.Map.empty[ActorRef, ContainerData]
  var prewarmedPool = immutable.Map.empty[ActorRef, PreWarmedData]
  // actor -> action kind, memory limit, core pin
  var prewarmStartingPool = immutable.Map.empty[ActorRef, (String, ByteSize, String)]
  //actor -> action, corePin
  var warmingPool = immutable.Map.empty[ActorRef, (ExecutableWhiskAction, String)]
  // If all memory slots are occupied and if there is currently no container to be removed, than the actions will be
  // buffered here to keep order of computation.
  // Otherwise actions with small memory-limits could block actions with large memory limits.
  var runBuffer = mutable.Map.empty[String, immutable.Queue[Run]]
  // Track the resent buffer head - so that we don't resend buffer head multiple times
  var resent = mutable.Map.empty[String, Option[Run]]
  val logMessageInterval = 10.seconds
  //periodically emit metrics (don't need to do this for each message!)
  context.system.scheduler.scheduleAtFixedRate(30.seconds, 10.seconds, self, EmitMetrics)

  var corePinStatus: mutable.Map[Int, Int] = mutable.Map(0 until Runtime.getRuntime.availableProcessors() map(_ -> 0): _*)

  // Key is ColdStartKey, value is the number of cold Start in minute
  var coldStartCount = immutable.Map.empty[ColdStartKey, Int]

  adjustPrewarmedContainer(true, false)

  // check periodically, adjust prewarmed container(delete if unused for some time and create some increment containers)
  // add some random amount to this schedule to avoid a herd of container removal + creation
  val interval = poolConfig.prewarmExpirationCheckInterval + poolConfig.prewarmExpirationCheckIntervalVariance
    .map(v =>
      Random
        .nextInt(v.toSeconds.toInt))
    .getOrElse(0)
    .seconds
  if (prewarmConfig.exists(!_.reactive.isEmpty)) {
    context.system.scheduler.scheduleAtFixedRate(
      poolConfig.prewarmExpirationCheckInitDelay,
      interval,
      self,
      AdjustPrewarmedContainer)
  }

  def logContainerStart(r: Run, containerState: String, activeActivations: Int, container: Option[Container]): Unit = {
    val namespaceName = r.msg.user.namespace.name.asString
    val actionName = r.action.name.name
    val actionNamespace = r.action.namespace.namespace
    val maxConcurrent = r.action.limits.concurrency.maxConcurrent
    val activationId = r.msg.activationId.toString

    r.msg.transid.mark(
      this,
      LoggingMarkers.INVOKER_CONTAINER_START(containerState, namespaceName, actionNamespace, actionName),
      s"containerStart containerState: $containerState container: $container activations: $activeActivations of max $maxConcurrent action: $actionName namespace: $namespaceName activationId: $activationId",
      akka.event.Logging.InfoLevel)
  }

  def getContainerForDeletionWithId(id: String): Option[(ActorRef, ContainerData)] = {
    (freePool ++ busyPool ++ prewarmedPool).find(entry =>
      entry._2.isInstanceOf[ContainerStarted] &&
        entry._2.asInstanceOf[ContainerStarted].container.containerId.asString == id)
  }

  def changeCoreLoad(core: Either[Int, String], increment: Boolean) = {
//    logging.info(this, s"changeCoreLoad: core:${core}")
    val coreNum = core match { case Left(c) => c; case Right(c) => c.toInt }
    corePinStatus.update(coreNum, corePinStatus(coreNum) + (if (increment) 1 else -1))
  }

  def takeLeastLoadedCoreOrCorePin(corePin: Option[String]): String = {
    corePin match {
      case Some(pin) if pin.trim.nonEmpty =>
        pin.split(",") foreach (core => changeCoreLoad(Right(core), true))
        pin
      case None =>
        val core = corePinStatus.minBy(_._2)._1
        changeCoreLoad(Left(core), true)
        core.toString
      case _ => ""
    }
  }

  def releaseCores(corePin: String) = {
    corePin.split(",") foreach (core => {if (core.nonEmpty) changeCoreLoad(Right(core), false)})
  }

  def getContainerForDeletionWithPriority(action: ExecutableWhiskAction, pools: Map[ActorRef, ContainerData]): Option[(ActorRef, ContainerData)] = {
    pools
      .find {
        case (_, _ @ WarmedData(_, _, `action`, _, _, _, _, _)) => true
        case (_, _ @ WarmingData(_, _, `action`, _, _, _)) => true
        case (_, _ @ WarmingColdData(_, `action`, _, _, _)) => true
        case _ => false
      }
  }

  def receive: Receive = {
    case w: BeginFullWarm =>
      if (hasPoolSpaceFor(busyPool ++ freePool ++ prewarmedPool, prewarmStartingPool, warmingPool, w.action.limits.memory.megabytes.MB, false)) {
        val newContainer = childFactory(context)
        val corePin = takeLeastLoadedCoreOrCorePin(Some(w.corePin))
        warmingPool = warmingPool + (newContainer -> (w.action, corePin))
        newContainer ! BeginFullWarm(w.action, corePin, w.params, w.transid)
      } else {
        logging.warn(
          this,
          s"Cannot create prewarm container due to reaching the invoker memory limit: ${poolConfig.userMemory.toMB}")
      }

    case DeleteRandomContainer(action) =>
      getContainerForDeletionWithPriority(action, freePool ++ busyPool) match {
        case Some(c) =>
          logging.info(this, s"deleting container id ${c._2.getContainer.get.containerId.asString}")
          removeContainer((c._1, c._2.corePin))
        case None =>
          logging.info(this, s"no warm containers for action ${action.name}, either free or busy, were found on this invoker to be deleted")
      }

    case DeleteContainerWithIdEvent(containerId) =>
      logging.info(this, s"Receive deletion rpc, starting deleting container $containerId")
      getContainerForDeletionWithId(containerId) match {
        case Some(c) =>
          removeContainer((c._1, c._2.corePin))
        case None =>
          logging.warn(this, s"could not find container $containerId when attempting to delete")
      }

    case SetAllowOpenWhiskToFreeMemoryEvent(setValue) =>
      allowOpenWhiskToFreeMemory = setValue
      logging.info(this, s"now ${if (setValue) "allowing" else "not allowing"} openwhisk to kill containers to free memory")

    case GetActionStates() =>
      sender() ! this.actionStates()

    case PrintRunBuffer() =>
      printCurrentRunBuffer()

    case PrintMetrics(origin) =>
      printMetrics(origin)

    case GetBufferedInvocationsEvent() =>
      sender() ! GetBufferedInvocationsResponse(runBuffer.map(
        buffer =>
          buffer._1 -> BufferedInvocationsPerFunction(buffer._2.map(_.msg.activationId.toString))
      ).toMap)

    case ResetInvokerEvent() =>
      logging.info(this, "resetting the invoker to startup state")
      feed ! MessageFeed.Reset
      runBuffer = mutable.Map.empty
      (freePool ++ busyPool ++ prewarmedPool).map(c => (c._1, c._2.corePin)) ++
        prewarmStartingPool.map(c => (c._1, c._2._3)) ++
        warmingPool.map(c => (c._1, c._2._2)) foreach removeContainer
      corePinStatus = corePinStatus.map(_._1 -> 0)

    // A job to run on a container
    //
    // Run messages are received either via the feed or from child containers which cannot process
    // their requests and send them back to the pool for rescheduling (this may happen if "docker" operations
    // fail for example, or a container has aged and was destroying itself when a new request was assigned)
    case r: Run =>
//      logging.info(this, "pool config")
//      println(poolConfig)
      // Check if the message is resent from the buffer. Only the first message on the buffer can be resent.
      val isResentFromBuffer = runBuffer.contains(r.action.name.name) && runBuffer(r.action.name.name).dequeueOption.exists(_._1.msg == r.msg)
      if (!isResentFromBuffer) { // if not resent from buffer, the activation must be a new arrival
        printMetrics("New Arrival:")
      }

      // Only process request, if there are no other requests waiting for free slots, or if the current request is the
      // next request to process
      // It is guaranteed, that only the first message on the buffer is resent.
      if ((if(runBuffer.contains(r.action.name.name)) runBuffer(r.action.name.name).isEmpty else true) || isResentFromBuffer) {
        if (isResentFromBuffer) {
          //remove from resent tracking - it may get resent again, or get processed
          resent.update(r.action.name.name, None)
        }
        val kind = r.action.exec.kind
        val memory = r.action.limits.memory.megabytes.MB

        def coldStartContainer(isHealthTest: Boolean, containerState: String): Option[((ActorRef, ContainerData), String)] = {
          if (hasPoolSpaceFor(busyPool ++ freePool ++ prewarmedPool, prewarmStartingPool, warmingPool, memory, isHealthTest)) {
            val container = Some(createContainer(memory), containerState)
            incrementColdStartCount(kind, memory)
            container
          } else None
        }

        val createdContainer =
          // Schedule a job to a warm container
          ContainerPool
            .schedule(r.action, r.msg.user.namespace.name, freePool)
            .map(container => (container, container._2.initingState)) //warmed, warming, and warmingCold always know their state
            .orElse {
              // There was no warm/warming/warmingCold container. Try to take a prewarm container or a cold container.
              // When take prewarm container, has no need to judge whether user memory is enough
              takePrewarmContainer(r.action)
                .map(container => (container, "prewarmed"))
                .orElse {
                  if (r.action.name.name.contains("invokerHealthTestAction")) coldStartContainer(true, "cold")
                  else if (poolConfig.enableColdStart &&
                    (poolConfig.alwaysColdStart ||
                      (!poolConfig.alwaysColdStart && !someContainerMatchesAction(r.action, r.msg.user.namespace.name, busyPool))))
                    coldStartContainer(false, "cold")
                  else None
                }
            }
            .orElse {
              if (allowOpenWhiskToFreeMemory && poolConfig.enableColdStart) {
                // Remove a container and create a new one for the given job
                logging.info(this, "deleting container to free memory")
                ContainerPool
                  // Only free up the amount, that is really needed to free up
                  .remove(freePool, Math.min(r.action.limits.memory.megabytes, memoryConsumptionOf(freePool)).MB)
                  .map(c => removeContainer(c._1, c._2.corePin))
                  // If the list had at least one entry, enough containers were removed to start the new container. After
                  // removing the containers, we are not interested anymore in the containers that have been removed.
                  .headOption
                  .map(_ =>
                    takePrewarmContainer(r.action)
                      .map(container => (container, "recreatedPrewarm"))
                      .getOrElse(coldStartContainer(false, "recreated").get))
              } else {
                logging.info(this,
                  s"currently disallowing openwhisk from killing containers to free memory, must reschedule invocation of function ${r.action.name.name}")
                None
              }
            }

        createdContainer match {
          case Some(((actor, data), containerState)) =>
            //increment active count before storing in pool map
            val newData = data.nextRun(r)
            val container = newData.getContainer

            if (newData.activeActivationCount < 1) {
              logging.error(this, s"invalid activation count < 1 ${newData}")
            }

            //only move to busyPool if max reached
            if (!newData.hasCapacity()) {
              if (r.action.limits.concurrency.maxConcurrent > 1) {
                logging.info(
                  this,
                  s"container ${container} is now busy with ${newData.activeActivationCount} activations")
              }
              busyPool = busyPool + (actor -> newData)
              freePool = freePool - actor
            } else {
              //update freePool to track counts
              freePool = freePool + (actor -> newData)
            }
            // Remove the action that was just executed from the buffer and execute the next one in the queue.
            if (isResentFromBuffer) {
              // It is guaranteed that the currently executed messages is the head of the queue, if the message comes
              // from the buffer
              val (_, newBuffer) = runBuffer(r.action.name.name).dequeue
              runBuffer.update(r.action.name.name, newBuffer)
              // Try to process the next item in buffer (or get another message from feed, if buffer is now empty)
              processBufferOrFeed()
            }
            actor ! Run(r.action, r.msg, takeLeastLoadedCoreOrCorePin(None), r.retryLogDeadline) // forwards the run request to the container
            logContainerStart(r, containerState, newData.activeActivationCount, container)
          case None =>
            // this can also happen if createContainer fails to start a new container, or
            // if a job is rescheduled but the container it was allocated to has not yet destroyed itself
            // (and a new container would over commit the pool)
            val isErrorLogged = r.retryLogDeadline.map(_.isOverdue).getOrElse(true)
            val retryLogDeadline = if (isErrorLogged) {
              logging.warn(
                this,
                s"Rescheduling Run message, too many message in the pool, " +
                  s"freePoolSize: ${freePool.size} containers and ${memoryConsumptionOf(freePool)} MB, " +
                  s"busyPoolSize: ${busyPool.size} containers and ${memoryConsumptionOf(busyPool)} MB, " +
                  s"maxContainersMemory ${poolConfig.userMemory.toMB} MB, " +
                  s"userNamespace: ${r.msg.user.namespace.name}, action: ${r.action}, " +
                  s"needed memory: ${r.action.limits.memory.megabytes} MB, " +
                  s"waiting messages: ${runBuffer.size}")(r.msg.transid)
              MetricEmitter.emitCounterMetric(LoggingMarkers.CONTAINER_POOL_RESCHEDULED_ACTIVATION)
              Some(logMessageInterval.fromNow)
            } else {
              r.retryLogDeadline
            }
            if (!isResentFromBuffer) {
              // Add this request to the buffer, as it is not there yet.
              runBuffer.update(r.action.name.name, runBuffer.getOrElseUpdate(r.action.name.name, immutable.Queue.empty).enqueue(Run(r.action, r.msg, r.corePin, retryLogDeadline)))
            }
          //buffered items will be processed via processBufferOrFeed()
        }
      } else {
        // There are currently actions waiting to be executed before this action gets executed.
        // These waiting actions were not able to free up enough memory.
        runBuffer.update(r.action.name.name, runBuffer.getOrElseUpdate(r.action.name.name, immutable.Queue.empty).enqueue(r))
        processBufferOrFeed()
      }

    // Container is free to take more work
    case NeedWork(warmData: WarmedData) =>
      printMetrics("need work, warmed data")
      if (!freePool.contains(sender()) && !busyPool.contains(sender())) {
        logging.info(this, s"needWork event, pools do not have container ${sender()}, ${warmData.container.containerId.asString}")
      }else {
        logging.info(this, s"needWork event, pools have container ${sender()}, ${warmData.container.containerId.asString}")
      
        val oldData = freePool.getOrElse(sender(), busyPool(sender()))
        val newData =
          warmData.copy(lastUsed = oldData.lastUsed, activeActivationCount = oldData.activeActivationCount - 1)
        if (newData.activeActivationCount < 0) {
          logging.error(this, s"invalid activation count after warming < 1 ${newData}")
        }
        if (newData.hasCapacity()) {
          //remove from busy pool (may already not be there), put back into free pool (to update activation counts)
          freePool = freePool + (sender() -> newData)
          if (busyPool.contains(sender())) {
            busyPool = busyPool - sender()
            if (newData.action.limits.concurrency.maxConcurrent > 1) {
              logging.info(
                this,
                s"concurrent container ${newData.container} is no longer busy with ${newData.activeActivationCount} activations")
            }
          }
        } else {
          busyPool = busyPool + (sender() -> newData)
          freePool = freePool - sender()
        }
        processBufferOrFeed()
      }

    case NeedWork(data: PreWarmedData) =>
      printMetrics("need work, prewarmed data")
      logging.info(this, s"need work event, prewarm ${sender()}, ${data.container.containerId.asString}")
      prewarmStartingPool = prewarmStartingPool - sender()
      prewarmedPool = prewarmedPool + (sender() -> data)
      processBufferOrFeed()

    case WarmCompleted(data: WarmedData) =>
      printMetrics("warm completed, warmed data")
      logging.info(this, s"warm completed event ${sender()}, ${data.container.containerId.asString}")
      warmingPool = warmingPool - sender()
      freePool = freePool + (sender() -> data)
      processBufferOrFeed()

    // Container got removed
    case ContainerRemoved(replacePrewarm) =>
      // if container was in free pool, it may have been processing (but under capacity),
      // so there is capacity to accept another job request
      freePool.get(sender()).foreach { f =>
        freePool = freePool - sender()
      }

      // container was busy (busy indicates at full capacity), so there is capacity to accept another job request
      busyPool.get(sender()).foreach { _ =>
        busyPool = busyPool - sender()
      }
      processBufferOrFeed()

      // in case this was a prewarm
      prewarmedPool.get(sender()).foreach { data =>
        prewarmedPool = prewarmedPool - sender()
      }

      // in case this was a starting prewarm
      prewarmStartingPool.get(sender()).foreach { _ =>
        logging.info(this, "failed starting prewarm, removed")
        prewarmStartingPool = prewarmStartingPool - sender()
      }

      warmingPool.get(sender()).foreach { _ =>
        logging.info(this, "failed to warm, removed")
        warmingPool = warmingPool - sender()
      }

      //backfill prewarms on every ContainerRemoved(replacePrewarm = true), just in case
      if (replacePrewarm) {
        adjustPrewarmedContainer(false, false) //in case a prewarm is removed due to health failure or crash
      }

    // This message is received for one of these reasons:
    // 1. Container errored while resuming a warm container, could not process the job, and sent the job back
    // 2. The container aged, is destroying itself, and was assigned a job which it had to send back
    // 3. The container aged and is destroying itself
    // Update the free/busy lists but no message is sent to the feed since there is no change in capacity yet
    case RescheduleJob =>
      freePool = freePool - sender()
      busyPool = busyPool - sender()
    case EmitMetrics =>
      emitMetrics()

    case AdjustPrewarmedContainer =>
      adjustPrewarmedContainer(false, true)
  }

  def printCurrentRunBuffer() = {
    logging.info(this, "printing run buffer:")
    //runBuffer.foreach(q => q._2.foreach(r => logging.info(this, s"${q._1}: ${r.action.name.name}")))
    runBuffer.foreach(q => logging.info(this, s"${q._1}: ${q._2.length}"))
    resent.foreach(flag => logging.info(this, s"${flag._1}: ${if (flag._2.isEmpty) "empty" else "hasValue"}"))
  }

  def printActionStates() = {
    logging.info(this, "printing action states")
    this.actionStates()._1.foreach(a => {
      logging.info(this, s"action: ${a._1.actionName}, state: ${a._1.state}, containers:")
      a._2.foreach(i => logging.info(this, i._1))
    })
  }

  def printMetrics(origin: String) = {
    logging.info(this, s"------------------------------ IMPORTANT METRICS (${origin}) ------------------------------")
    printCurrentRunBuffer()
    printActionStates()
    logging.info(this, s"------------------------------ IMPORTANT METRICS END (${origin}) ------------------------------")
  }

  /** Resend next item in the buffer, or trigger next item in the feed, if no items in the buffer. */
  def processBufferOrFeed() = {
    resent.foreach(flag =>logging.info(this, s"re-process buffer, resent flag: ${flag._1}: ${if (flag._2.isEmpty) "empty" else "hasValue"}"))
    if (runBuffer.isEmpty) feed ! MessageFeed.Processed

    runBuffer.foreach { buff =>
        // If buffer has more items, and head has not already been resent, send next one, otherwise get next from feed.
        buff._2.dequeueOption match {
          case Some((run, _)) => //run the first from buffer, (retrieve the first and the remaining of the elements)
            implicit val tid = run.msg.transid
            //avoid sending dupes
            if (resent.getOrElse(run.action.name.name, None).isEmpty) {
              logging.info(this, s"re-processing from buffer (${buff._2.length} ${run.action.name.name} items in buffer)")
              resent.update(run.action.name.name, Some(run))
              self ! run
            } else {
              //do not resend the buffer head multiple times (may reach this point from multiple messages, before the buffer head is re-processed)
              logging.info(this, s"re-process from buffer, do not resend the buffer, ${run.action.name.name}")
            }
          case None => //feed me!
            feed ! MessageFeed.Processed
            logging.info(this, s"re-process from buffer, empty buffer")
        }
    }
  }

  /** adjust prewarm containers up to the configured requirements for each kind/memory combination. */
  def adjustPrewarmedContainer(init: Boolean, scheduled: Boolean): Unit = {
    if (scheduled) {
      //on scheduled time, remove expired prewarms
      ContainerPool.removeExpired(poolConfig, prewarmConfig, prewarmedPool).foreach({
        (c: (ActorRef, PreWarmedData)) =>
        prewarmedPool = prewarmedPool - c._1
        releaseCores(c._2.corePin)
        c._1 ! Remove
      })
      //on scheduled time, emit cold start counter metric with memory + kind
      coldStartCount foreach { coldStart =>
        val coldStartKey = coldStart._1
        MetricEmitter.emitCounterMetric(
          LoggingMarkers.CONTAINER_POOL_PREWARM_COLDSTART(coldStartKey.memory.toString, coldStartKey.kind))
      }
    }
    //fill in missing prewarms (replaces any deletes)
    ContainerPool
      .increasePrewarms(init, scheduled, coldStartCount, prewarmConfig, prewarmedPool, prewarmStartingPool)
      .foreach { c =>
        val config = c._1
        val currentCount = c._2._1
        val desiredCount = c._2._2
        if (currentCount < desiredCount) {
          (currentCount until desiredCount).foreach { _ =>
            prewarmContainer(config.exec, config.memoryLimit, config.reactive.map(_.ttl), false)
          }
        }
      }
    if (scheduled) {
      //   lastly, clear coldStartCounts each time scheduled event is processed to reset counts
      coldStartCount = immutable.Map.empty[ColdStartKey, Int]
    }
  }

  /** Creates a new container and updates state accordingly. */
  def createContainer(memoryLimit: ByteSize): (ActorRef, ContainerData) = {
    val ref = childFactory(context)
    val data = MemoryData(memoryLimit)
    freePool = freePool + (ref -> data)
    logging.info(this, s"added new container to freePool ${sender()}")
    ref -> data
  }

  /** Creates a new prewarmed container */
  def prewarmContainer(exec: CodeExec[_], memoryLimit: ByteSize, ttl: Option[FiniteDuration], isHealthTestAction: Boolean): Unit = {
    if (hasPoolSpaceFor(busyPool ++ freePool ++ prewarmedPool, prewarmStartingPool, warmingPool, memoryLimit, isHealthTestAction)) {
      val newContainer = childFactory(context)
      val corePin = takeLeastLoadedCoreOrCorePin(None)
      prewarmStartingPool = prewarmStartingPool + (newContainer -> (exec.kind, memoryLimit, corePin))
      newContainer ! Start(exec, memoryLimit, corePin, ttl)
    } else {
      logging.warn(
        this,
        s"Cannot create prewarm container due to reach the invoker memory limit: ${poolConfig.userMemory.toMB}")
    }
  }

  /** this is only for cold start statistics of prewarm configs, e.g. not blackbox or other configs. */
  def incrementColdStartCount(kind: String, memoryLimit: ByteSize): Unit = {
    prewarmConfig
      .filter { config =>
        kind == config.exec.kind && memoryLimit == config.memoryLimit
      }
      .foreach { _ =>
        val coldStartKey = ColdStartKey(kind, memoryLimit)
        coldStartCount.get(coldStartKey) match {
          case Some(value) => coldStartCount = coldStartCount + (coldStartKey -> (value + 1))
          case None        => coldStartCount = coldStartCount + (coldStartKey -> 1)
        }
      }
  }

  /**
   * Takes a prewarm container out of the prewarmed pool
   * iff a container with a matching kind and memory is found.
   *
   * @param action the action that holds the kind and the required memory.
   * @return the container iff found
   */
  def takePrewarmContainer(action: ExecutableWhiskAction): Option[(ActorRef, ContainerData)] = {
    val kind = action.exec.kind
    val memory = action.limits.memory.megabytes.MB
    val now = Deadline.now
    prewarmedPool.toSeq
      .sortBy(_._2.expires.getOrElse(now))
      .find {
        case (_, PreWarmedData(_, `kind`, `memory`, _, _, _)) => true
        case _                                             => false
      }
      .map {
        case (ref, data) =>
          // Move the container to the usual pool
          freePool = freePool + (ref -> data)
          prewarmedPool = prewarmedPool - ref
          logging.info(this, s"added new container to freePool from prewarm ${sender()}, ${data.container.containerId.asString}")
          // Create a new prewarm container
          // NOTE: prewarming ignores the action code in exec, but this is dangerous as the field is accessible to the
          // factory

          //get the appropriate ttl from prewarm configs
          val ttl =
            prewarmConfig.find(pc => pc.memoryLimit == memory && pc.exec.kind == kind).flatMap(_.reactive.map(_.ttl))
          prewarmContainer(action.exec, memory, ttl, action.name.name.contains("invokerHealthTestAction"))
          (ref, data)
      }
  }

  /** Removes a container and updates state accordingly. */
  def removeContainer(c: (ActorRef, String)) = {
//    logging.info(this, "removing container")
    val (toDelete, corePin) = c
    toDelete ! Remove
    freePool = freePool - toDelete
    busyPool = busyPool - toDelete
    warmingPool = warmingPool - toDelete
    prewarmedPool = prewarmedPool - toDelete
    prewarmStartingPool = prewarmStartingPool - toDelete

    releaseCores(corePin)

    processBufferOrFeed()
  }

  /**
   * Calculate if there is enough free memory within a given pool.
   *
   * @param pool The pool, that has to be checked, if there is enough free memory.
   * @param memory The amount of memory to check.
   * @return true, if there is enough space for the given amount of memory.
   */
  def hasPoolSpaceFor[A](pool: Map[A, ContainerData],
                         prewarmStartingPool: Map[A, (String, ByteSize, String)],
                         warmingPool: Map[A, (ExecutableWhiskAction, String)],
                         memory: ByteSize,
                         isHealthTest: Boolean): Boolean = {
    memoryConsumptionOf(pool) + prewarmStartingPool.map(_._2._2.toMB).sum + warmingPool.map(_._2._1.limits.memory.megabytes.MB.toMB).sum + memory.toMB + (if (isHealthTest) 0 else poolConfig.healthTestBuffer) <= poolConfig.userMemory.toMB
  }

  def containerList(pool: Map[ActorRef, Any]): Iterable[(String, String)] = {
    pool collect {
      case (_, d: WarmedData) =>
        (d.container.containerId.asString, d.corePin)
      case (_, (_, pin: String)) =>
        ("", pin)
    }
  }

  private def actionState(actionName: String): List[(ContainerListKey, Iterable[(String, String)])] = {
    List(
      ContainerListKey(actionName, "free") -> containerList(freePool.filter(_._2.asInstanceOf[ContainerInUse].action.name.name == actionName)),
      ContainerListKey(actionName, "busy") -> containerList(busyPool.filter(_._2.asInstanceOf[ContainerInUse].action.name.name == actionName)),
      ContainerListKey(actionName, "warming") -> containerList(warmingPool.filter(_._2._1.name.name == actionName))
    )
  }

  def actionStates(): (Map[ContainerListKey, Iterable[(String, String)]], Long) = {
    //logging.info(this,"---->Collecting invoker state info")
    val actions: Set[String] = ((freePool ++ busyPool).map(_._2.asInstanceOf[ContainerInUse].action.name.name) ++ warmingPool.map(_._2._1.name.name)).toSet
    (actions.flatMap(actionState).toMap, freeMemoryMB())
  }

  private def freeMemoryMB(): Long = {
    val memoryUsed = List(freePool, busyPool, prewarmedPool).map(memoryConsumptionOf).sum + prewarmStartingPool.map(_._2._2.toMB).sum + warmingPool.map(_._2._1.limits.memory.megabytes.MB.toMB).sum
    poolConfig.userMemory.toMB - memoryUsed
  }

  /**
   * Log metrics about pool state (buffer size, buffer memory requirements, active number, active memory, prewarm number, prewarm memory)
   */
  private def emitMetrics() = {
    MetricEmitter.emitGaugeMetric(LoggingMarkers.CONTAINER_POOL_RUNBUFFER_COUNT, runBuffer.size)
    MetricEmitter.emitGaugeMetric(
      LoggingMarkers.CONTAINER_POOL_RUNBUFFER_SIZE,
      runBuffer.flatMap(_._2.map(_.action.limits.memory.megabytes)).sum)
    val containersInUse = freePool.filter(_._2.activeActivationCount > 0) ++ busyPool
    MetricEmitter.emitGaugeMetric(LoggingMarkers.CONTAINER_POOL_ACTIVE_COUNT, containersInUse.size)
    MetricEmitter.emitGaugeMetric(
      LoggingMarkers.CONTAINER_POOL_ACTIVE_SIZE,
      containersInUse.map(_._2.memoryLimit.toMB).sum)
    MetricEmitter.emitGaugeMetric(
      LoggingMarkers.CONTAINER_POOL_PREWARM_COUNT,
      prewarmedPool.size + prewarmStartingPool.size)
    MetricEmitter.emitGaugeMetric(
      LoggingMarkers.CONTAINER_POOL_PREWARM_SIZE,
      prewarmedPool.map(_._2.memoryLimit.toMB).sum + prewarmStartingPool.map(_._2._2.toMB).sum)
    val unused = freePool.filter(_._2.activeActivationCount == 0)
    val unusedMB = unused.map(_._2.memoryLimit.toMB).sum
    MetricEmitter.emitGaugeMetric(LoggingMarkers.CONTAINER_POOL_IDLES_COUNT, unused.size)
    MetricEmitter.emitGaugeMetric(LoggingMarkers.CONTAINER_POOL_IDLES_SIZE, unusedMB)
  }
}

object ContainerPool {

  /**
   * Calculate the memory of a given pool.
   *
   * @param pool The pool with the containers.
   * @return The memory consumption of all containers in the pool in Megabytes.
   */
  protected[containerpool] def memoryConsumptionOf[A](pool: Map[A, ContainerData]): Long = {
    pool.map(_._2.memoryLimit.toMB).sum
  }

  /**
   * Finds the best container for a given job to run on.
   *
   * Selects an arbitrary warm container from the passed pool of idle containers
   * that matches the action and the invocation namespace. The implementation uses
   * matching such that structural equality of action and the invocation namespace
   * is required.
   * Returns None iff no matching container is in the idle pool.
   * Does not consider pre-warmed containers.
   *
   * @param action the action to run
   * @param invocationNamespace the namespace, that wants to run the action
   * @param idles a map of idle containers, awaiting work
   * @return a container if one found
   */
  protected[containerpool] def schedule[A](action: ExecutableWhiskAction,
                                           invocationNamespace: EntityName,
                                           idles: Map[A, ContainerData]): Option[(A, ContainerData)] = {
    idles
      .find {
        case (_, c @ WarmedData(_, `invocationNamespace`, `action`, _, _, _, _, _)) if c.hasCapacity() => true
        case _                                                                                   => false
      }
      .orElse {
        idles.find {
          case (_, c @ WarmingData(_, `invocationNamespace`, `action`, _, _, _)) if c.hasCapacity() => true
          case _                                                                                 => false
        }
      }
      .orElse {
        idles.find {
          case (_, c @ WarmingColdData(`invocationNamespace`, `action`, _, _, _)) if c.hasCapacity() => true
          case _                                                                                  => false
        }
      }
  }

  protected[containerpool] def someContainerMatchesAction[A](action: ExecutableWhiskAction,
                                                      invocationNamespace: EntityName,
                                                      pool: Map[A, ContainerData]): Boolean = {
    pool.exists {
      case (_, _ @ WarmedData(_, `invocationNamespace`, `action`, _, _, _, _, _)) => true
      case (_, _ @ WarmingData(_, `invocationNamespace`, `action`, _, _, _)) => true
      case (_, _ @ WarmingColdData(`invocationNamespace`, `action`, _, _, _)) => true
      case _ => false
    }
  }

  /**
   * Finds the oldest previously used container to remove to make space for the job passed to run.
   * Depending on the space that has to be allocated, several containers might be removed.
   *
   * NOTE: This method is never called to remove an action that is in the pool already,
   * since this would be picked up earlier in the scheduler and the container reused.
   *
   * @param pool a map of all free containers in the pool
   * @param memory the amount of memory that has to be freed up
   * @return a list of containers to be removed iff found
   */
  @tailrec
  protected[containerpool] def remove[A](pool: Map[A, ContainerData],
                                         memory: ByteSize,
                                         toRemove: List[(A, ContainerData)] = List.empty): List[(A, ContainerData)] = {
    // Try to find a Free container that does NOT have any active activations AND is initialized with any OTHER action
    val freeContainers = pool.collect {
      // Only warm containers will be removed. Prewarmed containers will stay always.
      case (ref, w: WarmedData) if w.activeActivationCount == 0 =>
        ref -> w
    }

    if (memory > 0.B && freeContainers.nonEmpty && memoryConsumptionOf(freeContainers) >= memory.toMB) {
      // Remove the oldest container if:
      // - there is more memory required
      // - there are still containers that can be removed
      // - there are enough free containers that can be removed
      val (ref, data) = freeContainers.minBy(_._2.lastUsed)
      // Catch exception if remaining memory will be negative
      val remainingMemory = Try(memory - data.memoryLimit).getOrElse(0.B)
      remove(freeContainers - ref, remainingMemory, toRemove ++ List((ref, data)))
    } else {
      // If this is the first call: All containers are in use currently, or there is more memory needed than
      // containers can be removed.
      // Or, if this is one of the recursions: Enough containers are found to get the memory, that is
      // necessary. -> Abort recursion
      toRemove
    }
  }

  /**
   * Find the expired actor in prewarmedPool
   *
   * @param poolConfig
   * @param prewarmConfig
   * @param prewarmedPool
   * @param logging
   * @return a list of expired actor
   */
  def removeExpired[A](poolConfig: ContainerPoolConfig,
                       prewarmConfig: List[PrewarmingConfig],
                       prewarmedPool: Map[A, PreWarmedData])(implicit logging: Logging): List[(A, PreWarmedData)] = {
    val now = Deadline.now
    val expireds = prewarmConfig
      .flatMap { config =>
        val kind = config.exec.kind
        val memory = config.memoryLimit
        config.reactive
          .map { c =>
            val expiredPrewarmedContainer = prewarmedPool.toSeq
              .filter { warmInfo =>
                warmInfo match {
                  case (_, p @ PreWarmedData(_, `kind`, `memory`, _, _, _)) if p.isExpired() => true
                  case _                                                                  => false
                }
              }
              .sortBy(_._2.expires.getOrElse(now))

            if (expiredPrewarmedContainer.nonEmpty) {
              // emit expired container counter metric with memory + kind
              MetricEmitter.emitCounterMetric(LoggingMarkers.CONTAINER_POOL_PREWARM_EXPIRED(memory.toString, kind))
              logging.info(
                this,
                s"[kind: ${kind} memory: ${memory.toString}] ${expiredPrewarmedContainer.size} expired prewarmed containers")
            }
            expiredPrewarmedContainer.map(e => (e._1, e._2))
          }
          .getOrElse(List.empty)
      }
      .sortBy(_._2.expires.getOrElse(now)) //need to sort these so that if the results are limited, we take the oldest
    if (expireds.nonEmpty) {
      logging.info(this, s"removing up to ${poolConfig.prewarmExpirationLimit} of ${expireds.size} expired containers")
      expireds.take(poolConfig.prewarmExpirationLimit).foreach { e =>
        prewarmedPool.get(e._1).map { d =>
          logging.info(this, s"removing expired prewarm of kind ${d.kind} with container ${d.container} ")
        }
      }
    }
    expireds.take(poolConfig.prewarmExpirationLimit)
  }

  /**
   * Find the increased number for the prewarmed kind
   *
   * @param init
   * @param scheduled
   * @param coldStartCount
   * @param prewarmConfig
   * @param prewarmedPool
   * @param prewarmStartingPool
   * @param logging
   * @return the current number and increased number for the kind in the Map
   */
  def increasePrewarms(init: Boolean,
                       scheduled: Boolean,
                       coldStartCount: Map[ColdStartKey, Int],
                       prewarmConfig: List[PrewarmingConfig],
                       prewarmedPool: Map[ActorRef, PreWarmedData],
                       prewarmStartingPool: Map[ActorRef, (String, ByteSize, String)])(
    implicit logging: Logging): Map[PrewarmingConfig, (Int, Int)] = {
    prewarmConfig.map { config =>
      val kind = config.exec.kind
      val memory = config.memoryLimit

      val runningCount = prewarmedPool.count {
        // done starting (include expired, since they may not have been removed yet)
        case (_, p @ PreWarmedData(_, `kind`, `memory`, _, _, _)) => true
        // started but not finished starting (or expired)
        case _ => false
      }
      val startingCount = prewarmStartingPool.count(p => p._2._1 == kind && p._2._2 == memory)
      val currentCount = runningCount + startingCount

      // determine how many are needed
      val desiredCount: Int =
        if (init) config.initialCount
        else {
          if (scheduled) {
            // scheduled/reactive config backfill
            config.reactive
              .map(c => getReactiveCold(coldStartCount, c, kind, memory).getOrElse(c.minCount)) //reactive -> desired is either cold start driven, or minCount
              .getOrElse(config.initialCount) //not reactive -> desired is always initial count
          } else {
            // normal backfill after removal - make sure at least minCount or initialCount is started
            config.reactive.map(_.minCount).getOrElse(config.initialCount)
          }
        }

      if (currentCount < desiredCount) {
        logging.info(
          this,
          s"found ${currentCount} started and ${startingCount} starting; ${if (init) "initing" else "backfilling"} ${desiredCount - currentCount} pre-warms to desired count: ${desiredCount} for kind:${config.exec.kind} mem:${config.memoryLimit.toString}")(
          TransactionId.invokerWarmup)
      }
      (config, (currentCount, desiredCount))
    }.toMap
  }

  /**
   * Get the required prewarmed container number according to the cold start happened in previous minute
   *
   * @param coldStartCount
   * @param config
   * @param kind
   * @param memory
   * @return the required prewarmed container number
   */
  def getReactiveCold(coldStartCount: Map[ColdStartKey, Int],
                      config: ReactivePrewarmingConfig,
                      kind: String,
                      memory: ByteSize): Option[Int] = {
    coldStartCount.get(ColdStartKey(kind, memory)).map { value =>
      // Let's assume that threshold is `2`, increment is `1` in runtimes.json
      // if cold start number in previous minute is `2`, requireCount is `2/2 * 1 = 1`
      // if cold start number in previous minute is `4`, requireCount is `4/2 * 1 = 2`
      math.min(math.max(config.minCount, (value / config.threshold) * config.increment), config.maxCount)
    }
  }

  def props(factory: ActorRefFactory => ActorRef,
            poolConfig: ContainerPoolConfig,
            feed: ActorRef,
            prewarmConfig: List[PrewarmingConfig] = List.empty)(implicit logging: Logging) =
    Props(new ContainerPool(factory, feed, prewarmConfig, poolConfig))

}
  
/** Contains settings needed to perform container prewarming. */
case class PrewarmingConfig(initialCount: Int,
                            exec: CodeExec[_],
                            memoryLimit: ByteSize,
                            reactive: Option[ReactivePrewarmingConfig] = None)

case class GetActionStates()
case class PrintRunBuffer()
case class PrintMetrics(origin: String)
