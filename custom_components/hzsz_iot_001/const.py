"""Constants for the HZSZ_IOT_001 Thing Model integration."""

DOMAIN = "hzsz_iot_001"

MANUFACTURER = "HZSZ"
SW_VERSION = "Bluetooth"

# ---- 物模型 API 默认地址 ----
# Java 后端统一前缀为 /admin-api，物模型公开接口在 /iot/thing-model 下
DEFAULT_THING_MODEL_URL = "https://itcm.hzshuzi.com"
THING_MODEL_API = "/admin-api/iot/thing-model/get-by-model"

# ---- MQTT 默认配置 ----
DEFAULT_MQTT_BROKER = "192.168.1.210"
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_USERNAME = "admin"
DEFAULT_MQTT_PASSWORD = "hzsz@#2021a"
QOS = 1

# ---- MQTT Topic ----
# 使用 + 通配符匹配网关 SN，主题统一带前导 /
TOPIC_UPLINK = "/hzsz/gateway/+/UplinkData"
TOPIC_REGISTER = "/hzsz/gateway/+/Register"
TOPIC_HEARTBEAT = "/hzsz/gateway/+/Heartbeat"

# ---- 离线检测间隔 ----
OFFLINE_CHECK_INTERVAL = 60  # 秒
OFFLINE_TIMEOUT_MINUTES = 10

# ---- 已知设备（静态注册）----
# 已弃用静态注册：所有设备均通过 MQTT Register 或 UplinkData 动态发现。
# 支持的型号通过 Java API list-models 接口动态获取，不在此处写死。
KNOWN_DEVICES: dict[str, dict[str, str]] = {}
