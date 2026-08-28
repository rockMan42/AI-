from pymilvus import MilvusClient

from app.config.settings import Settings

milvus_client: MilvusClient = None

async def init_milvus(settings: Settings):
    """初始化milvus"""
    global milvus_client

    # 防重复创建
    if milvus_client is not None:
        return milvus_client

    milvus_uri = f"http://{settings.milvus_host}:{settings.milvus_port}"

    try:
        milvus_client = MilvusClient(uri=milvus_uri)
        milvus_client.list_collections()
        print(f"Milvus连接成功:{milvus_uri}")
    except Exception as e:
        print(f"milvus初始化失败:{e}")
        raise e

    return milvus_client

async def close_milvus():
    """关闭milvus连接"""
    global milvus_client

    try:
        if milvus_client is not None:
            milvus_client.close()
            print("milvus安全关闭！")
    except Exception as e:
        print(f"关闭连接失败:{e}")

async def check_milvus():
    global milvus_client

    try:
        if milvus_client is None:
            return "check_milvus init fail!"
        milvus_client.list_collections()
        return "connected"
    except Exception as e:
        print(f"error:{e}")
        raise e