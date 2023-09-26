package org.apache.openwhisk.core.loadBalancer.grpc

import akka.actor.ActorSystem
import akka.grpc.GrpcClientSettings
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.core.loadBalancer.RPCHeuristicLoadBalancerConfig
import org.apache.openwhisk.grpc._

import java.time.Instant
import scala.collection.mutable
import scala.concurrent.{ExecutionContextExecutor, Future}
import scala.util.Failure

class RoutingClient(lbConfig: RPCHeuristicLoadBalancerConfig)(implicit actorSystem: ActorSystem, logging: Logging) {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher
  private val clientSettings: GrpcClientSettings = GrpcClientSettings.connectToServiceAt(lbConfig.agentIp, lbConfig.routingPort).withTls(false)
  val client: RoutingService = RoutingServiceClient(clientSettings)

  def executeRoutingRequest(actionName: String, activationId: String): (Future[GetInvocationRouteResponse], Instant) = {
    logging.info(this, s"executing routing request for action: $actionName")
    val time = Instant.now()
    (client.getInvocationRoute(GetInvocationRouteRequest(actionName, activationId)), time)
  }

  def executeRoutingClusterStateUpdate(state: mutable.Map[Int, ActionStatePerInvoker]): Unit = {
    logging.info(this, "executing clusterstate request from clusterStateUpdate client")
    val reply = client.routingUpdateClusterState(UpdateClusterStateRequest(Some(InvokerClusterState(state.toMap))))
    reply.onComplete {
//      case Success(value) =>
//        logging.info(this, s"updating cluster state request has succeeded")
      case Failure(e) =>
        logging.info(this, s"updating cluster state request has failed ${e.getMessage}")
      case _ =>
    }
  }
}
