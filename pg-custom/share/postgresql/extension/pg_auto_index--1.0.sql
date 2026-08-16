/* contrib/pg_auto_index/pg_auto_index--1.0.sql */

\echo Use "CREATE EXTENSION pg_auto_index" to load SQL helpers only. The workload logger must be loaded with shared_preload_libraries. \quit

CREATE FUNCTION pg_auto_index_version()
RETURNS text
AS 'MODULE_PATHNAME'
LANGUAGE C STRICT PARALLEL SAFE;
