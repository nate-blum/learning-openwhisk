package org.apache.openwhisk.core.loadBalancer.grpc

import akka.actor.ActorSystem
import akka.grpc.GrpcClientSettings
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.core.loadBalancer.RPCHeuristicLoadBalancerConfig
import org.apache.openwhisk.grpc._

import scala.concurrent.duration.DurationInt
import scala.concurrent.{Await, ExecutionContextExecutor, Future}

class ClusterStateClient(lbConfig: RPCHeuristicLoadBalancerConfig)(implicit actorSystem: ActorSystem, logging: Logging) {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher
  private val clientSettings: GrpcClientSettings = GrpcClientSettings.connectToServiceAt(lbConfig.agentIp, lbConfig.clusterStatePort).withTls(false)
  val client: ClusterStateService = ClusterStateServiceClient(clientSettings)

  def executeClusterStateUpdate(state: InvokerClusterState): UpdateClusterStateResponse = {
    logging.info(this, "executing routing request")
    val request: Future[UpdateClusterStateResponse] = client.updateClusterState(UpdateClusterStateRequest(Some(state)))
    Await.result(request, 10.seconds)
  }



}
