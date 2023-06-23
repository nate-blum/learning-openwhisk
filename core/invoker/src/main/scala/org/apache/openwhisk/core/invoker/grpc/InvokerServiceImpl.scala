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

package org.apache.openwhisk.core.invoker.grpc;

import akka.actor.ActorSystem
import akka.pattern.ask
import akka.util.Timeout
import org.apache.openwhisk.common.{Logging, TransactionId}
import org.apache.openwhisk.core.WarmUp
import org.apache.openwhisk.core.connector.{ActivationMessage, Message}
import org.apache.openwhisk.core.entity.{DocRevision, FullyQualifiedEntityName}
import org.apache.openwhisk.core.scheduler.queue._
import org.apache.openwhisk.grpc.{ActivationService, FetchRequest, FetchResponse, RescheduleRequest, RescheduleResponse}
import spray.json._

import scala.concurrent.duration._
import scala.concurrent.{ExecutionContextExecutor, Future}
import scala.util.Try

class InvokerServiceImpl()(implicit actorSystem: ActorSystem, logging: Logging) extends InvokerService {
  implicit val requestTimeout: Timeout = Timeout(5.seconds)
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher

  override def NewPrewarmedContainer(request: NewPrewarmedContainerRequest): Future[NewPrewarmedContainerResponse] = {
    logging.info(this, s"Trying to create a new prewarmed container.")
  }
}

object InvokerServiceImpl {
  def apply()(implicit actorSystem: ActorSystem, logging: Logging) =
    new InvokerServiceImpl()
}