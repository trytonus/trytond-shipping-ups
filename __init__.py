# -*- coding: utf-8 -*-
"""
    __init__.py

"""
from trytond.pool import Pool
from party import Address
from carrier import Carrier, CarrierService, BoxType
from sale import Configuration, Sale
from configuration import PartyConfiguration
from stock import (
    ShipmentOut, StockMove, ShippingUps, GenerateShippingLabel, Package
)


def register():
    Pool.register(
        PartyConfiguration,
        Address,
        Carrier,
        CarrierService,
        BoxType,
        Configuration,
        Sale,
        StockMove,
        ShipmentOut,
        ShippingUps,
        Package,
        module='shipping_ups', type_='model'
    )

    Pool.register(
        GenerateShippingLabel,
        module='shipping_ups', type_='wizard'
    )
