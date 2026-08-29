from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

def success_response(message: str = "响应成功", data = None):
    content = {
        "code":200,
        "message":message,
        "data":data
    }
    return JSONResponse(content=jsonable_encoder(content))