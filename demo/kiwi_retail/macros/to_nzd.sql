{# Convert an amount in `currency_col` (NZD or AUD) to NZD at the fixed planning rate. #}
{% macro to_nzd(amount, currency_col) -%}
    round(cast({{ amount }} * case when {{ currency_col }} = 'AUD' then {{ var('aud_to_nzd') }} else 1 end as {{ dbt.type_numeric() }}), 2)
{%- endmacro %}
