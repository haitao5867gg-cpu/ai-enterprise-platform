#!/bin/sh
(
set -eu

# Hex encoding keeps even quotes, backslashes and newlines in the configured
# password out of SQL syntax. The same script seeds the MySQL CI service.
reader_password_hex=$(printf '%s' "$AEP_MYSQL_PASSWORD" | od -An -tx1 | tr -d ' \n')
MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket --user=root <<SQL
SET @reader_password = CONVERT(UNHEX('$reader_password_hex') USING utf8mb4);
SET @create_reader = CONCAT('CREATE USER ''source_reader''@''%'' IDENTIFIED BY ', QUOTE(@reader_password));
PREPARE create_reader FROM @create_reader;
EXECUTE create_reader;
DEALLOCATE PREPARE create_reader;
GRANT SELECT ON sales.* TO 'source_reader'@'%';
SQL
)
