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
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.core.containerpool.CreateNewPrewarmedContainerEvent
import org.apache.openwhisk.core.invoker.{InvokerCore, InvokerReactive}
import org.apache.openwhisk.grpc.{InvokerService, NewPrewarmedContainerRequest, NewPrewarmedContainerResponse}

import scala.concurrent.{ExecutionContextExecutor, Future}

class InvokerServiceImpl(invokerRef: InvokerCore)(implicit actorSystem: ActorSystem, logging: Logging) extends InvokerService {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher

  override def newPrewarmedContainer(request: NewPrewarmedContainerRequest): Future[NewPrewarmedContainerResponse] = {
    invokerRef.asInstanceOf[InvokerReactive].handleInvokerRPCEvent(CreateNewPrewarmedContainerEvent)
    Future.successful(NewPrewarmedContainerResponse(true))
  }
}

object InvokerServiceImpl {
  def apply(invokerRef: InvokerCore)(implicit actorSystem: ActorSystem, logging: Logging) =
    new InvokerServiceImpl(invokerRef)
}