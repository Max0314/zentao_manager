-- 给 bi_reader 开启 zentaoep 库所有表的只读权限。
-- 注意：这里是 zentaoep.*，不是 *.*，不会授权读取其他数据库。
-- 把 YOUR_CLIENT_OR_SERVER_IP 换成实际连接 MariaDB 的机器 IP。
-- 当前电脑测试时，MariaDB 识别到的来源 IP 是 10.3.55.21。

CREATE USER IF NOT EXISTS 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP' IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';

GRANT SELECT ON zentaoep.* TO 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP';

FLUSH PRIVILEGES;

SHOW GRANTS FOR 'bi_reader'@'YOUR_CLIENT_OR_SERVER_IP';
