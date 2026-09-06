-- Docker init script: creates both databases on first start.
-- Runs automatically when the container is initialised from a fresh volume.

\c flow

-- Create the test database if it does not already exist.
SELECT 'CREATE DATABASE flow_test OWNER pgsql'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'flow_test')
\gexec
