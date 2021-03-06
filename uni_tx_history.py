#!/usr/bin/env python
# encoding=utf-8
import json
import requests
import redis
import time
import pymysql
import logging
from config.env import CONFIG
from service import redis_client as redis_conn
from db import mysqldb

logger = logging.getLogger(__name__)
logger.setLevel(level=logging.DEBUG)

handler = logging.StreamHandler()
# formatter = logging.Formatter('%(asctime)s %(filename)s %(lineno)s %(message)s')
formatter = logging.Formatter('%(asctime)s %(message)s')
handler.setFormatter(formatter)

logger.addHandler(handler)

UNI_CONTRACT = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"

def sync_uni_v2_his_info():
    """
    同步uni合约历史记录
    起始同步高度: 10207858
    """
    node = CONFIG['node']
    table = CONFIG['table']
    # 已同步的高度
    uni_sync_his_number_key = "uni_his_already_synced_number"
    # 已同步得交易数量
    uni_already_synced_tx_count_key = "uni_already_synced_tx_count"

    config = CONFIG['mysql']
    connection = pymysql.connect(**config)

    # 每次请求的块数,动态调整
    interval = 5

    while True:
        data = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
        res = requests.post(node, json=data)
        result = res.json()
        # 查询当前最新高度
        new_block_num = int(result.get("result", "0"), base=16)
        # 延迟2个区块解析数据,防止分叉情况
        new_block_num = new_block_num - 2

        # 初始化已同步的高度
        synced_block_number = redis_conn.get(uni_sync_his_number_key)
        if not synced_block_number:
            already_synced = int(new_block_num) - 10
        else:
            already_synced = int(synced_block_number)

        # 2021-05-16发现问题: interval至少从2开始,若interval=1则可能出现end_block=start_block相导致无限等待的情况
        interval = max(2, interval)
        start_block = already_synced + 1
        end_block = min(already_synced+interval, new_block_num)
        # 若已追到最新区块则等会儿
        if start_block >= end_block:
            logger.info(f"[waiting]: interval:{interval}, start_block:{start_block}, end_block:{end_block}")
            interval = 1
            time.sleep(30)
            continue
        txs = []
        for num in range(start_block, end_block):
            block_num = hex(int(num))
            try:
                data = {"jsonrpc": "2.0", "method": "eth_getBlockByNumber", "params": [block_num, True], "id": 1}
                res = requests.post(node, json=data)
                result = res.json()
                datas = result.get("result")
                block_hash = datas.get("hash")
                timestamp = datas.get("timestamp")
                if timestamp:
                    tx_time = int(str(timestamp), base=16)
                else:
                    tx_time = 123456
                transactions = datas.get("transactions", [])
                if not transactions:
                    continue
                for tx in transactions:
                    v_from = tx.get("from", "")
                    if not v_from:
                        continue
                    v_to = tx.get("to", "")
                    if not v_to:
                        continue
                    v_to_str = v_to.lower()
                    if v_to_str != UNI_CONTRACT:
                        continue

                    txid = tx.get("hash")
                    if not txid:
                        continue
                    tmp = {
                        "token_name": "uni",
                        "block_height": num,
                        "block_hash": block_hash,
                        "tx_hash": txid,
                        "timestamp": tx_time,
                    }
                    txs.append(tmp)
                already_synced = num
            except Exception as e:
                logger.info(e)
                break
        logger.info(f"interval:{interval}, start_block:{start_block}, end_block:{end_block}")
        if not txs:
            interval += 5
            redis_conn.set(uni_sync_his_number_key, already_synced)
            continue
        txs_count = len(txs)
        if txs_count < 100:
            interval += 1
        else:
            interval -= 1
        sync_block_count = end_block - start_block
        logger.info(f"start:{start_block}, end:{end_block}, block_count:{sync_block_count}, get tx_count:{txs_count}")
        try:
            values = ",".join(["('{token_name}', {block_height}, '{block_hash}', '{tx_hash}', '{timestamp}')".format(**one) for one in txs])
            cursor = connection.cursor()
            sql = f"""
               INSERT IGNORE INTO {table}(`token_name`, `block_height`, `block_hash`, `tx_hash`, `timestamp`) values {values};
            """
            cursor.execute(sql)
            connection.commit()
        except Exception as e:
            logger.info(f"syncing block:{already_synced}")
            logger.info(e)
            continue
        redis_conn.set(uni_sync_his_number_key, already_synced)
        redis_conn.incrby(uni_already_synced_tx_count_key, txs_count)

        time.sleep(0.2)


if __name__ == "__main__":
    sync_uni_v2_his_info()
