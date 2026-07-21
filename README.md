# router-scan

用于扫描路由器客户端状态并记录目标设备活动的 Python 程序。

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

首次运行前，请在 `monitor/config.py` 中填写本地路由器登录信息。不要提交真实密码、推送令牌、Cookie 或程序生成的状态文件。
