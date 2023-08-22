package org.apache.openwhisk.core.loadBalancer.grpc

import akka.actor.ActorSystem
import akka.grpc.GrpcClientSettings
import org.apache.openwhisk.common.Logging
import org.apache.openwhisk.core.loadBalancer.RPCHeuristicLoadBalancerConfig
import org.apache.openwhisk.grpc._

import scala.collection.mutable
import scala.concurrent.ExecutionContextExecutor
import scala.util.{Failure, Success}

class ClusterStateClient(lbConfig: RPCHeuristicLoadBalancerConfig)(implicit actorSystem: ActorSystem, logging: Logging) {
  implicit val ec: ExecutionContextExecutor = actorSystem.dispatcher
  private val clientSettings: GrpcClientSettings = GrpcClientSettings.connectToServiceAt(lbConfig.agentIp, lbConfig.clusterStatePort).withTls(false)
  val client: ClusterStateService = ClusterStateServiceClient(clientSettings)

  def executeClusterStateUpdate(state: mutable.Map[Int, ActionStatePerInvoker]): Unit = {
    val reply = client.updateClusterState(UpdateClusterStateRequest(Some(InvokerClusterState(state.toMap))))
    reply.onComplete {
      case Failure(e) =>
        logging.info(this, s"updating cluster state request has failed ${e.getMessage}")
      case _ =>
    }
  }



}
