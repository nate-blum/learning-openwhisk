package org.apache.openwhisk.core.loadBalancer.grpc

import akka.actor.ActorSystem
import akka.grpc.GrpcClientSettings
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.core.loadBalancer.RPCHeuristicLoadBalancerConfig
import org.apache.openwhisk.grpc._

import scala.collection.mutable
import scala.concurrent.duration.DurationInt
import scala.concurrent.{Await, ExecutionContextExecutor, Future}

class RoutingClient(lbConfig: RPCHeuristicLoadBalancerConfig)(implicit actorSystem: ActorSystem, logging: Logging) {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher
  private val clientSettings: GrpcClientSettings = GrpcClientSettings.connectToServiceAt(lbConfig.agentIp, lbConfig.routingPort).withTls(false)
  val client: RoutingService = RoutingServiceClient(clientSettings)

  def executeRoutingRequest(actionName: String): GetInvocationRouteResponse = {
    logging.info(this, "executing routing request")
    val request: Future[GetInvocationRouteResponse] = client.getInvocationRoute(GetInvocationRouteRequest(actionName))
    Await.result(request, 10.seconds)
  }

  def executeClusterStateUpdateRouting(state: mutable.Map[Int, ActionStatePerInvoker]): UpdateClusterStateResponse = {
    logging.info(this, "executing clusterstate request")
    val request: Future[UpdateClusterStateResponse] = client.routingUpdateClusterState(UpdateClusterStateRequest(Some(InvokerClusterState(state.toMap))))
    Await.result(request, 10.seconds)
  }
}
