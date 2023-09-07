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

package org.apache.openwhisk.core.invoker.grpc

import akka.actor.ActorSystem
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.core.invoker.{InvokerCore, InvokerReactive}
import org.apache.openwhisk.grpc.{DeleteContainerWithIdRequest, DeleteRandomContainerRequest, EmptyRequest, GetBufferedInvocationsResponse, InvokerService, NewWarmedContainerRequest, ResetInvokerRequest, SetAllowOpenWhiskToFreeMemoryRequest, SuccessResponse}

import scala.concurrent.{ExecutionContextExecutor, Future};


class InvokerServiceImpl(invokerRef: InvokerCore)(implicit actorSystem: ActorSystem, logging: Logging) extends InvokerService {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher

  def handleEvent(event: InvokerRPCEvent): Future[Any] = {
    invokerRef.asInstanceOf[InvokerReactive].handleInvokerRPCEvent(event)
  }

  override def newWarmedContainer(request: NewWarmedContainerRequest): Future[SuccessResponse] = {
    handleEvent(NewWarmedContainerEvent(request.actionName, "guest", request.corePin, request.params.map { case (k, v) => (k, Set(v))}))
    Future.successful(SuccessResponse(true))
  }

  override def deleteRandomContainer(request: DeleteRandomContainerRequest): Future[SuccessResponse] = {
    handleEvent(DeleteRandomContainerEvent(request.actionName, "guest"))
    Future.successful(SuccessResponse(true))
  }

  override def deleteContainerWithId(request: DeleteContainerWithIdRequest): Future[SuccessResponse] = {
    handleEvent(DeleteContainerWithIdEvent(request.containerId))
    Future.successful(SuccessResponse(true))
  }

  override def setAllowOpenWhiskToFreeMemory(request: SetAllowOpenWhiskToFreeMemoryRequest): Future[SuccessResponse] = {
    handleEvent(SetAllowOpenWhiskToFreeMemoryEvent(request.setValue))
    Future.successful(SuccessResponse(true))
  }

  override def resetInvoker(request: ResetInvokerRequest): Future[SuccessResponse] = {
    handleEvent(ResetInvokerEvent())
    Future.successful(SuccessResponse(true))
  }

  override def getBufferedInvocations(in: EmptyRequest): Future[GetBufferedInvocationsResponse] = {
    handleEvent(GetBufferedInvocationsEvent()).map(_.asInstanceOf[GetBufferedInvocationsResponse])
  }
}

object InvokerServiceImpl {
  def apply(invokerRef: InvokerCore)(implicit actorSystem: ActorSystem, logging: Logging) =
    new InvokerServiceImpl(invokerRef)
}

// new warmed container event - received by ContainerPool
trait InvokerRPCEvent
case class NewWarmedContainerEvent(actionName: String, namespace: String, corePin: String, params: Map[String, Set[String]]) extends InvokerRPCEvent
case class DeleteRandomContainerEvent(actionName: String, namespace: String) extends InvokerRPCEvent
case class DeleteContainerWithIdEvent(containerId: String) extends InvokerRPCEvent
case class SetAllowOpenWhiskToFreeMemoryEvent(setValue: Boolean) extends InvokerRPCEvent
case class ResetInvokerEvent() extends InvokerRPCEvent
case class GetBufferedInvocationsEvent() extends InvokerRPCEvent