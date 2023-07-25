import datetime
def get_curr_time():
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M-%S")
if __name__ == "__main__":
    print(get_curr_time())