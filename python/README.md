Important Configuration Variables:
Invoker:
1. ansible/roles/invoker/tasks/deploy.yml -> "expose additional ports if jmxremote is enabled"
    the list contains the ports to be exposed on the invoker's docker container (50051:50051 is the current rpc port)
2. ansible/group_vars/all AND ansible/environments/local/group_vars/all (make sure to set the same values in both)
    managedFraction - the fraction of invokers which will be "managed" (used for actions with known runtimes)
    blackboxFraction - the fraction of invokers which will be "blackbox" (used for actions without known runtimes)
    agentIp - the IP address that the controller will query for any agent requests
    sendAllUpdateRequests - decides whether to send every single cluster update to the agent, or just updates with changes
    routingPort - the port the controller will query for routing decisions
    clusterStatePort - the port the controller will send cluster state updates to