import subprocess
from requests_futures.sessions import FuturesSession


class OpenwhiskClient:
    NUM_WORKER = 4
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

    def invoke_binary_data(self, action: str, data):
        self.session.post(url=self.base_gust_url + action, headers={"Content-Type": "image/jpeg"}, data=data,
                          verify=False) # async call

    def invoke_common(self, action:str):
        self.session.post(url = self.base_gust_url + action, verify= False)
