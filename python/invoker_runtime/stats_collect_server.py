import logging
import rpyc
import time
import subprocess
import threading
from rpyc.utils.server import ThreadedServer
import pickle

class StatService(rpyc.Service):
    DEL_THRESHOLD_SEC = 10
    DEL_THREAD_PERIOD_SEC = 10
    def __init__(self):
        self.container_2_utilization: dict[str, tuple[float, float]] = {}
        self.update_lock = threading.Lock()
        self.reading_thread = threading.Thread(target=self._threaded_pull_container_stat, args=(), daemon=True)
        self.del_inactive_container_thread = threading.Thread(target=self._thread_del_inactive_container, daemon=True)
        self.reading_thread.start()
        self.del_inactive_container_thread.start()
        self.sub_process = None
        print("Docker stats collector server started", flush=True)

    def _threaded_pull_container_stat(self):
        self.sub_process = subprocess.Popen(["docker", "stats", "--format", "{{.ID}} {{.CPUPerc}}", "--no-trunc"],
                                            stdout=subprocess.PIPE, text=True)
        print("Collecting Subprocess id:", self.sub_process.pid, flush=True)
        while True:
            for line in self.sub_process.stdout:
                line_arr = line.strip().split()
                t = time.time()
                with self.update_lock:
                    # get rid of the ANSI terminal control sequenc [7:]
                    self.container_2_utilization[line_arr[0][7:]] = (float(line_arr[1].rstrip('%')) / 100, t)
                    #print(time.time(),line.strip()[7:],flush=True)

    def _thread_del_inactive_container(self):
        while True:
            container_lst = list(self.container_2_utilization.keys())
            t = time.time()
            for container in container_lst:
                with self.update_lock:
                    if t - self.container_2_utilization[container][1] > self.DEL_THRESHOLD_SEC: # more than 10s has no update
                        del self.container_2_utilization[container]
                        logging.info(f"Delete inactive container: {container}")
            time.sleep(self.DEL_THREAD_PERIOD_SEC)

    def exposed_get_container_utilization(self):
        # return Immutable type to avoid Netref, if use netref not sure how the locking is gonna work, be on the safe side
        with self.update_lock:
            return pickle.dumps(self.container_2_utilization)


def test_subprocess():
    import time
    p = subprocess.Popen(["docker", "stats", "--format", "{{.ID}} {{.CPUPerc}}"], stdout=subprocess.PIPE, text=True)
    while True:
        for line in p.stdout:
            print(line.strip(), time.time())


if __name__ == "__main__":
    service = ThreadedServer(StatService(), port=18861)
    service.start()
