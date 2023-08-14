from docker.models.containers import Container
import docker
import threading

def stats(container: Container):
    for stat in container.stats(decode=True, stream=True):
        print(container.name, stat)

def main():
    client = docker.from_env()
    threads = dict()
    for container in client.containers.list():
        threads[container.id] = threading.Thread(target=stats, args=(container,))
        threads[container.id].start()
    
if __name__ == "__main__":
    main()