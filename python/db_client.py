import requests
import config_local
import json

class DB:
    DB_CONFIG_FILE = config_local.db_config_file  #

    def __init__(self):
        self.configs = self.GetDBConfigs()
        self.url_find = self.configs['db_protocol'] + '://' + self.configs['db_host'] + ':' + \
                        self.configs['db_port'] + '/' + 'whisk_local_activations/_find'

    @staticmethod
    def GetDBConfigs():
        """
        Retrieves DB configs from a configuration file.
        """
        configs = {}
        with open(DB.DB_CONFIG_FILE, 'r') as config_file:
            lines = config_file.readlines()
            for line in lines:
                if line[0] == '[':
                    domain = line[1:-2]
                    configs[domain] = {}
                    last_dom = domain
                else:
                    try:
                        key = line[:line.index('=')]
                    except:
                        continue
                    configs[last_dom][key] = line[line.index('=') + 1:-1]

        return configs['db_creds']

    def GetActivationRecordsSince(self,since, until, limit=100000):
        """
        Returns details on activation records since a given tick in milliseconds
        """
        headers = {
            'Content-Type': 'application/json',
        }
        # body = {
        #     "selector": {
        #         "start": {
        #             "$gte": since
        #         }
        #     },
        #     "limit": limit
        # }
        body = {
            "selector": {
                "$and": [
                    {"start": {"$gte": since}},
                    {"start": {"$lte": until}}
                ]
            },
            "limit": limit
        }

        respond:requests.Response = requests.post(self.url_find, json=body, headers=headers,
                      auth=(self.configs['db_username'], self.configs['db_password']))

        print(respond.text, respond.status_code)
if __name__=='__main__':
    import time
    db =DB()
    db.GetActivationRecordsSince(int(time.time()) - 10, int(time.time()))
