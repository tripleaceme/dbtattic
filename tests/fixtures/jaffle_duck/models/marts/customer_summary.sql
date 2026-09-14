{{ config(materialized='table') }}

with customers as (select * from {{ ref('stg_customers') }}),
     orders as (select * from {{ ref('customer_orders') }})

select
    c.customer_id,
    c.first_name,
    c.last_name,
    count(o.order_id)              as number_of_orders,
    coalesce(sum(o.order_total),0) as lifetime_value
from customers c
left join orders o on c.customer_id = o.customer_id
group by 1, 2, 3
