import logging
from collections import deque
import rpyc
import time
import subprocess
from subprocess import check_output
import threading
from rpyc.utils.server import ThreadedServer
import pickle
from statistics import mean


class StatService(rpyc.Service):
    DEL_THRESHOLD_SEC = 10
    DEL_THREAD_PERIOD_SEC = 10
    RECORD_NUM = 2  # record is generated around 0.5 second each

    def __init__(self):
        self.container_2_utilization: dict[str, deque[tuple[float, float]]] = {}
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
                container_id = line_arr[0][7:] if line_arr[0][:7] =="\x1b[2J\x1b[H" else line_arr[0]
                if len(line_arr) < 2:
                    logging.error(f"Line array size  < 2, {line}")
                    continue
                t = time.time()
                with self.update_lock:
                    if container_id not in self.container_2_utilization:
                        self.container_2_utilization[container_id] = deque(maxlen=self.RECORD_NUM)
                    # get rid of the ANSI terminal control sequenc [7:]
                    self.container_2_utilization[container_id].append((float(line_arr[1].rstrip('%')) / 100, t))
                    # print(time.time(),line.strip()[7:],flush=True)

    def _thread_del_inactive_container(self):
        while True:
            with self.update_lock:
                container_lst = list(self.container_2_utilization.keys())
            t = time.time()
            for container in container_lst:
                with self.update_lock:
                    if t - self.container_2_utilization[container][-1][
                        1] > self.DEL_THRESHOLD_SEC:  # check the newest record in queue, if more than 10s has no update
                        del self.container_2_utilization[container]
                        logging.info(f"Delete inactive container: {container}")
            time.sleep(self.DEL_THREAD_PERIOD_SEC)

    def exposed_get_container_ids(self):
        try:
            out = check_output(["docker", "ps", "--format", "{{.ID}} {{.Image}} {{.Names}}", "--no-trunc"])
        except Exception as e:
            logging.error(f"Get container ids error: {e}")
            return None
        out_list = out.splitlines()
        function_id_lst = []
        for line in out_list:
            line_arr = line.split()
            if line_arr[1].decode()[:13] == 'whisk/invoker' or line_arr[2].decode()[:7] =='invoker':
                continue
            function_id_lst.append(line_arr[0].decode())
        return pickle.dumps(function_id_lst)

    def exposed_get_container_utilization(self):
        # return Immutable type to avoid Netref, if use netref not sure how the locking is gonna work, be on the safe side
        with self.update_lock:
            #return pickle.dumps(self.container_2_utilization)
            return pickle.dumps({k:mean([i[0] for i in v]) for k, v in self.container_2_utilization.items()})

    def exposed_reset(self):
        with self.update_lock:
            self.container_2_utilization.clear()


def test_subprocess():
    import time
    p = subprocess.Popen(["docker", "stats", "--format", "{{.ID}} {{.CPUPerc}}"], stdout=subprocess.PIPE, text=True)
    while True:
        for line in p.stdout:
            print(line.strip(), time.time())
def test():
    StatService().expose_get_container_ids()

if __name__ == "__main__":
    service = ThreadedServer(StatService(), port=18861)
    service.start()
