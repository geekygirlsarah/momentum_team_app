from django import template

from travel.utils import travel_description_for_display

register = template.Library()


@register.filter
def travel_description(value):
    return travel_description_for_display(value)
