-- The connector's account: a SQL login whose database user may only SELECT from the
-- sales schema. aep-sqlserver-seed fills in $(READER_PASSWORD) from AEP_SQLSERVER_PASSWORD
-- and runs this file every time, so a changed password is applied.
USE source;
GO
IF SUSER_ID('source_reader') IS NULL
    CREATE LOGIN source_reader WITH PASSWORD = '$(READER_PASSWORD)', CHECK_POLICY = OFF,
        DEFAULT_DATABASE = source;
ELSE
    ALTER LOGIN source_reader WITH PASSWORD = '$(READER_PASSWORD)';
GO
IF USER_ID('source_reader') IS NULL
    CREATE USER source_reader FOR LOGIN source_reader;
GO
GRANT SELECT ON SCHEMA::sales TO source_reader;
