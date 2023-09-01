Important Configuration Variables:
- Invoker:
1. `ansible/roles/invoker/tasks/deploy.yml` -> "expose additional ports if jmxremote is enabled"
    the list contains the ports to be exposed on the invoker's docker container (50051:50051 is the current rpc port)
2. `/core/invoker/src/main/resources/application.conf` ->
    `health-test-buffer` - the buffer (in MB) to maintain in free memory to ensure that health test actions can be run
    `always-cold-start` - true -> always cold start even if busy container exists for action, false -> wait for busy containers, else cold start if there are none (does not apply if enable-cold-start is false)
    `enable-cold-start` - true -> allow openwhisk to cold start containers on its own, false -> disable this functionality
    `cluster-state-ping-interval` - interval between cluster state update pings
- Controller:
1. `ansible/group_vars/all` AND `ansible/environments/local/group_vars/all` (make sure to set the same values in both)
    `managedFraction` - the fraction of invokers which will be "managed" (used for actions with known runtimes)
    `blackboxFraction` - the fraction of invokers which will be "blackbox" (used for actions without known runtimes)
2. `agentIp` - the IP address that the controller will query for any agent requests
3. `sendAllUpdateRequests` - decides whether to send every single cluster update to the agent, or just updates with changes
4. `routingPort` - the port the controller will query for routing decisions
5. `clusterStatePort` - the port the controller will send cluster state updates to

# TODO
- Make sure invoker id from Wsk runtime match agent runtime (e.g., invoker0 is xe?).
- How to Reset cluster for each function
- **Check every feature & stats are reset proper at each step**
- check if all the update are thread safe
- **Rethink invocation routing algorithm**
- Config the PDU

# TESTED
- add single container
- select core to pin based on core pin load given existing containers pinnings
# TODO TEST:
- auto cold start

# Different From Simulator
- Simulator function selection is P95, reward is P99 <--> Real wsk all use p99
- When select the active function, the usage of latency tail: only finished record, within a window <----> include both waiting
and finished invocation
# EXECUTION ORDER:
Assuming cold-start only occurs when no free container, A, B... means different function container or request.
1. Current Queue: `A->B->A`, what if currently a B container is ready/free? Would B get executed? Or only when A at head is dispatched can B be dispatched.
   - E.g., Current Queue:`A->B->A`, invoker currently has one `A_busy` container and `B_free` container. Would B get executed event A is at the head.
2. Current Queue: `A`, invoker currently has one `A_busy` container and one `B_free` container. Now a request of B arrives, would B get executed right away or
buffered in the Queue
3. Current Queue is `A`, invoker currently has one `A_busy` container, and now a request of B
arrives, a cold start is supposed to happen for the B request, then the cold start begin. Would the request be buffered in queue or just waiting the warming container to dispatch to.