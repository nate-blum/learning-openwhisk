package org.apache.openwhisk.core.loadBalancer.grpc

import akka.actor.ActorSystem
import akka.grpc.GrpcClientSettings
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.grpc._

import scala.concurrent.duration.DurationInt
import scala.concurrent.{Await, ExecutionContextExecutor, Future}

class ControllerClient()(implicit actorSystem: ActorSystem, logging: Logging) {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher
  private val clientSettings: GrpcClientSettings = GrpcClientSettings.connectToServiceAt("127.0.0.1", 50051).withTls(false)
  val client: ControllerService = ControllerServiceClient(clientSettings)

  def executeRoutingRequest(actionName: String): GetInvocationRouteResponse = {
    logging.info(this, "executing routing request")
    val request: Future[GetInvocationRouteResponse] = client.getInvocationRoute(GetInvocationRouteRequest(actionName))
    Await.result(request, 10.seconds)
  }

}
