import logging

import requests
import config_local
import couchdb
import json


class CouchDB_Py:
    DB_CONFIG_FILE = config_local.db_config_file

    def __init__(self):
        self.configs = self.GetDBConfigs()
        self.server = couchdb.Server(
            f"{self.configs['db_protocol']}://{self.configs['db_username']}:{self.configs['db_password']}@{self.configs['db_host']}:{self.configs['db_port']}/")
        self.db = self.server['whisk_local_activations']
        print("CouchDB version:", self.server.version())

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

    def find(self, since, limit) -> map:
        body = {
            "selector": {
                "start": {
                    "$gte": since
                }
            },
            "limit": limit
        }

        res = self.db.find(mango_query=body)
        return res

    def delete(self, doc):
        self.db.delete(doc)

    def delete_activation_db(self):
        self.server.delete('whisk_local_activations')
        logging.info("Successfully deleted whisk_local_activation")

class DB:
    DB_CONFIG_FILE = config_local.db_config_file  #

    def __init__(self):
        self.configs = self.GetDBConfigs()
        self.url_find = self.configs['db_protocol'] + '://' + self.configs['db_host'] + ':' + \
                        self.configs['db_port'] + '/' + 'whisk_local_activations/_find'
        self.url_index = self.configs['db_protocol'] + '://' + self.configs['db_host'] + ':' + \
                         self.configs['db_port'] + '/' + 'whisk_local_activations/_index'

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

    def GetActivationRecordsSince(self, since, until, limit=100000):
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
        #     # "fields": ["start", "end", "duration", "name", "annotations"]
        # }
        body = {
            "selector": {
                "$and": [
                    {"start": {"$gte": since}},
                    {"start": {"$lt": until}}
                ]
            },
            "limit": limit
        }

        respond: requests.Response = requests.post(self.url_find, json=body, headers=headers,
                                                   auth=(self.configs['db_username'], self.configs['db_password']))

        return respond.json()
        # return json.loads(respond.content)
        # print(json.loads(respond.content), respond.status_code)

    def GetActivationRecordsSinceStart(self, since, limit=100000):
        """
        Returns details on activation records since a given tick in milliseconds
        """
        headers = {
            'Content-Type': 'application/json',
        }
        body = {
            "selector": {
                {"start":
                     {"$gte": since}
                 }
            },
            "limit": limit
        }

        respond: requests.Response = requests.post(self.url_find, json=body, headers=headers,
                                                   auth=(self.configs['db_username'], self.configs['db_password']))

        return respond.json()
        # return json.loads(respond.content)
        # print(json.loads(respond.content), respond.status_code)

    def GetActivationRecordsEndTimeSinceUntil(self, since, end, limit=100000):
        # query based on end time
        headers = {
            'Content-Type': 'application/json',
        }
        body = {
            "selector": {
                "$and": [
                    {"end": {"$gt": since}},
                    {"end": {"$lte": end}}
                ]
            },
            "limit": limit
        }

        respond: requests.Response = requests.post(self.url_find, json=body, headers=headers,
                                                   auth=(self.configs['db_username'], self.configs['db_password']))

        return respond.json()

    def create_index(self, field_lst: list[str]):

        headers = {
            'Content-Type': 'application/json',
        }
        body = {
            "index": {
                "fields": field_lst
            },
            "name": "end-index",
            "type": "json"
        }
        resp = requests.post(self.url_index, json=body, headers=headers,
                             auth=(self.configs['db_username'], self.configs['db_password']))
        logging.info(f"Index created: {resp}")


if __name__ == '__main__':
    import time

    db = DB()
    #db.create_index(['end'])
    db_python = CouchDB_Py()
    #db_python.delete_activation_db()
    t = time.time()
    res = db.GetActivationRecordsEndTimeSinceUntil((int(time.time()) - 3600 * 72) * 1000, int(time.time()) * 1000)
    #res = db_python.find((int(time.time()) - 3600 * 60) * 1000, 1_000_000)
    t_end = time.time()
    count = 0
    # for i in res:
    #     count += 1
    #     print(i)
    #
    # print("count:", count)

    for record in res['docs']:
        print({k:v for k, v in record.items() if k != 'response'})
    print(t_end - t)
