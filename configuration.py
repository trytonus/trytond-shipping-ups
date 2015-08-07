# -*- coding: utf-8 -*-
"""
    Configuration

"""
from trytond.pool import PoolMeta

__all__ = ['PartyConfiguration']
__metaclass__ = PoolMeta


class PartyConfiguration:
    "Party Configuration"
    __name__ = 'party.configuration'

    @classmethod
    def get_carrier_methods_for_domain(cls):
        """
        UPS can be used for address validation. So add to the list.
        """
        res = super(PartyConfiguration, cls).get_carrier_methods_for_domain()
        if 'ups' not in res:
            res.append('ups')
        return res
