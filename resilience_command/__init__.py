"""灾后多制式通信恢复指挥服务的服务端包入口。"""

PROJECT_CODE = "resilience_command"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识，供运行检查和诊断使用。"""
    return {"code": PROJECT_CODE, "title": "灾后多制式通信恢复指挥服务"}
