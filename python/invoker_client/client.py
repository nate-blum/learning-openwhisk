import getopt
import sys
import grpc
import invoker_pb2 as invoker_types
import invoker_pb2_grpc as invoker_service

state = {}

def main():
    opts, args = getopt.getopt(sys.argv[1:], 'h:')
    for opt, arg in opts:
        if opt == '-h':
            state['host'] = arg

    channel = grpc.insecure_channel(state['host'])
    stub = invoker_service.InvokerServiceStub(channel)

    # synchronous calls
    for i in range(1):
        stub.NewWarmedContainer(
            invoker_types.NewWarmedContainerRequest(
                actionName="helloPy", params={
                "--cpuset-cpus": ",".join([str(x) for x in [i, i + 8]])
            }))
    stub.SetAllowOpenWhiskToFreeMemory(setValue=False)
    stub.DeleteContainer(actionName="helloPy")

    # asynchronous call
    future = stub.SetAllowOpenWhiskToFreeMemory.future(actionName="helloPy")
    result = future.result()
    print("result = " + result)

if __name__ == "__main__":
    main()