Running Instructions :-

1. First launch start_pg_custom.sh. It directly connects to a database `test`. test DB already contains a relation `demo_orders` with columns order_id, customer_id, region_id, status, order_date, amount and notes.

2. Launch start_daemon.sh in another terminal.

3. Run your queries in first terminal. For multiple queries that are similar, as per the query jumbling logic of postgres, indices will be created by daemon

4. To see the logging of queries you can see file pg_auto_index.csv inside postgres/data/