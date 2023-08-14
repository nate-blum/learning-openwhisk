from typing import Optional
import logging
from statistics import mean
from threading import Lock, Thread
from easysnmp import Session
from easysnmp.exceptions import EasySNMPTimeoutError
from easysnmp.exceptions import EasySNMPError
import time

class PDU_reader:
    def __init__(self, pdu_ip, outlet_id_lst, sample_interval):
        self.pdu_ip = pdu_ip
        self.outlet_id_lst = outlet_id_lst
        self.active_power_oids = ['.1.3.6.1.4.1.13742.6.5.4.3.1.4.1.{}.5'.format(i) for i in outlet_id_lst]
        self.active_energy_oids = ['.1.3.6.1.4.1.13742.6.5.4.3.1.4.1.{}.8'.format(i) for i in outlet_id_lst]
        self.snmp_sess = Session(hostname=self.pdu_ip, community='public', version=2)
        self.sample_interval = sample_interval  # in second
        self.power_sample_lock = Lock()
        self.power_sample: list[float] = list()
        self.pdu_thread:Optional[Thread] = None

    def clear_samples(self):
        with self.power_sample_lock:
            self.power_sample.clear()

    def get_average_power(self):
        with self.power_sample_lock:
            return mean(self.power_sample)

    def query_pdu_active_power(self) -> Optional[list[int]]:
        try:
            res = self.snmp_sess.get(self.active_power_oids)
        except EasySNMPTimeoutError:
            logging.critical("SNMP pull PDU power Timeout")
            return None
        except EasySNMPError:
            return None
        else:
            act_powers = [int(i.value) for i in res]
            return act_powers

    def query_pdu_active_energy(self) -> Optional[list[int]]:
        try:
            res = self.snmp_sess.get(self.active_energy_oids)
        except EasySNMPTimeoutError:
            logging.critical("SNMP pull PDU Energy Timeout")
            return None
        except EasySNMPError:
            return None
        else:
            active_energy = [int(i.value) for i in res]
            return active_energy

    def start_thread(self):
        self.pdu_thread = Thread(target=self.threaded_collect_pdu, args=(), daemon=True)
        self.pdu_thread.start()
        return self.pdu_thread

    def threaded_collect_pdu(self):
        while True:
            start_t = time.time()
            res = self.query_pdu_active_power()
            with self.power_sample_lock:
                self.power_sample.append(sum(res))
            duration = time.time() - start_t
            if duration < self.sample_interval:
                time.sleep(self.sample_interval - duration)


if __name__ == "__main__":
    import time

    pdu = PDU_reader('panic-pdu-01.cs.rutgers.edu', [15, 16], 0.5)
    begin = time.time()
    while time.time() - begin < 3:
        t = time.time()
        res = pdu.query_pdu_active_power()
        print(time.time(), res)
