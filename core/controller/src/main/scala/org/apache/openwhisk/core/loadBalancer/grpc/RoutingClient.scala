package org.apache.openwhisk.core.loadBalancer.grpc

import akka.actor.ActorSystem
import akka.grpc.GrpcClientSettings
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.core.loadBalancer.RPCHeuristicLoadBalancerConfig
import org.apache.openwhisk.grpc._

import scala.collection.mutable
import scala.concurrent.duration.DurationInt
import scala.concurrent.{Await, ExecutionContextExecutor}
import scala.util.{Try, Success, Failure}

class RoutingClient(lbConfig: RPCHeuristicLoadBalancerConfig)(implicit actorSystem: ActorSystem, logging: Logging) {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher
  private val clientSettings: GrpcClientSettings = GrpcClientSettings.connectToServiceAt(lbConfig.agentIp, lbConfig.routingPort).withTls(false)
  val client: RoutingService = RoutingServiceClient(clientSettings)

  def executeRoutingRequest(actionName: String): Option[GetInvocationRouteResponse] = {
    logging.info(this, s"executing routing request for action: $actionName")
    val request: Try[GetInvocationRouteResponse] = Await.ready(client.getInvocationRoute(GetInvocationRouteRequest(actionName)), 10.seconds).value.get
    request match {
      case Success(value) =>
        Some(value)
      case Failure(e) =>
        logging.info(this, s"executing routing request request has failed ${e.getMessage} for action $actionName")
        None
    }
  }

  def executeClusterStateUpdateRouting(state: mutable.Map[Int, ActionStatePerInvoker]): Option[UpdateClusterStateResponse] = {
    logging.info(this, "executing clusterstate request")
    val request: Try[UpdateClusterStateResponse] =
      Await.ready(client.routingUpdateClusterState(UpdateClusterStateRequest(Some(InvokerClusterState(state.toMap)))), 10.seconds).value.get
    request match {
      case Success(value) =>
        Some(value)
      case Failure(e) =>
        logging.info(this, s"updating cluster state request has failed ${e.getMessage}")
        None
    }
  }
}
