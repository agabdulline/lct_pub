"""Ключи Redis, через которые процессы backend обмениваются состоянием."""

CLOCK_SIM = "clock:sim"  # SimulatedTime эмулятора, ISO 8601
CLOCK_SIM_RECEIVED_AT = "clock:sim_received_at"  # когда gateway получил значение, unix time
CLOCK_RUN = "clock:run_id"  # прогон, к которому SimulatedTime относится по метке записи; "" — без прогона
GATEWAY_STATUS = "gateway:status"  # connected / disconnected
GATEWAY_STATS = "gateway:stats"  # счётчики шлюза и текущий прогон источника, JSON
WORKER_HEARTBEAT = "worker:heartbeat"  # последний пульс worker, unix time
