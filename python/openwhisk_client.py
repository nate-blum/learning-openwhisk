import subprocess
import urllib3
urllib3.disable_warnings()
from requests_futures.sessions import FuturesSession
from concurrent.futures import ProcessPoolExecutor
from requests import Session



class OpenwhiskClient:
    NUM_WORKER = 1000
    WSK_PATH = "/usr/local/bin/wsk"

    def __init__(self, wsk_path: str):
        self.wsk_bin_path: str = self.WSK_PATH
        APIHOST = subprocess.check_output(self.wsk_bin_path + " property get --apihost", shell=True).split()[3]
        APIHOST = "https://" + APIHOST.decode("utf-8")
        NAMESPACE = subprocess.check_output(self.wsk_bin_path + " property get --namespace", shell=True).split()[2]
        NAMESPACE = NAMESPACE.decode("utf-8")
        self.base_gust_url = APIHOST + "/api/v1/web/guest/default/"
        #self.base_url = APIHOST + "/api/v1/namespaces/" + NAMESPACE + "/actions/"
        self.session = FuturesSession(max_workers=self.NUM_WORKER)
        #self.session = FuturesSession(executor=ProcessPoolExecutor(max_workers=8), session=Session())

    def invoke_binary_data(self, action: str, data):
        self.session.post(url=self.base_gust_url + action, headers={"Content-Type": "image/jpeg"}, data=data,
                          verify=False) # async call

    def invoke_common(self, action:str):
        self.session.post(url = self.base_gust_url + action, verify= False)
