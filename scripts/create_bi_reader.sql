-- 创建 bi_reader 并只授权当前服务第一版需要的三张表。
-- 把 YOUR_CLIENT_OR_SERVER_IP 换成实际连接 MariaDB 的机器 IP。
-- 当前电脑测试时，MariaDB 识别到的来源 IP 是 10.3.55.21。
-- 最终 Docker 部署时，应换成运行 zentao_manager 的内网服务器 IP。

CREATE USER IF NOT EXISTS 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP' IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';

GRANT SELECT ON zentaoep.zt_action TO 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP';
GRANT SELECT ON zentaoep.zt_user TO 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP';
GRANT SELECT ON zentaoep.zt_dept TO 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP';

FLUSH PRIVILEGES;

SHOW GRANTS FOR 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP';
