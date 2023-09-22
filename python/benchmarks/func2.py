import time

def main(args):
    i = 0
    start_t = time.time()
    while True:
        i += 1
        if time.time() - start_t > 1:
            break
    return {"result": "finished func2"}
