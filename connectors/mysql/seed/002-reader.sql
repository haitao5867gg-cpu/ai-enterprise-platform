-- The image creates MYSQL_USER (source_reader) with MYSQL_PASSWORD and ALL on
-- MYSQL_DATABASE before running this file. Narrow that grant to SELECT only.
-- Plain SQL keeps the seed working where bind-mounted scripts cannot be
-- executed (Docker Desktop on macOS), and the password never enters SQL text.
REVOKE ALL PRIVILEGES ON `sales`.* FROM 'source_reader'@'%';
GRANT SELECT ON `sales`.* TO 'source_reader'@'%';
