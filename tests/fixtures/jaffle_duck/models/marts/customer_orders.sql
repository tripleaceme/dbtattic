{{ config(materialized='table') }}

with orders as (select * from {{ ref('stg_orders') }}),
     payments as (select * from {{ ref('stg_payments') }}),

order_totals as (
    select order_id, sum(amount) as order_total
    from payments
    group by order_id
)

select
    o.order_id,
    o.customer_id,
    o.order_date,
    o.status,
    coalesce(t.order_total, 0) as order_total
from orders o
left join order_totals t on o.order_id = t.order_id
