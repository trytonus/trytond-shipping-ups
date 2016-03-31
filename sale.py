# -*- coding: utf-8 -*-
"""
    sale.py

"""
from decimal import Decimal
from logbook import Logger

from lxml import etree
from lxml.builder import E
from ups.rating_package import RatingService
from ups.base import PyUPSException
from trytond.model import fields
from trytond.pool import PoolMeta, Pool

__all__ = ['Configuration', 'Sale']
__metaclass__ = PoolMeta

logger = Logger('trytond_ups')


class Configuration:
    'Sale Configuration'
    __name__ = 'sale.configuration'

    ups_box_type = fields.Many2One(
        'carrier.box_type', 'UPS Box Type', required=True,
        domain=[
            'OR',
            [('carrier_cost_method', '=', 'ups')],
            [('carrier_cost_method', '=', None)],
        ]
    )

    @staticmethod
    def default_ups_box_type():
        # This is the default value as specified in UPS doc
        ModelData = Pool().get('ir.model.data')

        return ModelData.get_id("shipping_ups", "ups_02")


class Sale:
    "Sale"
    __name__ = 'sale.sale'

    ups_saturday_delivery = fields.Boolean("Is Saturday Delivery")

    @staticmethod
    def default_ups_saturday_delivery():
        return False

    def get_shipping_rate(self, carrier, carrier_service=None, silent=False):
        Currency = Pool().get('currency.currency')

        if carrier.carrier_cost_method != 'ups':
            return super(Sale, self).get_shipping_rate(
                carrier, carrier_service, silent
            )

        rate_request = self._get_rate_request_xml(carrier, carrier_service)
        rate_api = carrier.ups_api_instance(call="rate")

        # Logging.
        logger.debug(
            'Making Rate API Request for shipping rates of'
            'Sale ID: {0} and Carrier ID: {1}'
            .format(self.id, carrier.id)
        )
        logger.debug(
            '--------RATE API REQUEST--------\n%s'
            '\n--------END REQUEST--------'
            % etree.tostring(rate_request, pretty_print=True)
        )

        try:
            response = rate_api.request(rate_request)
            # Logging.
            logger.debug(
                '--------START RATE API RESPONSE--------\n%s'
                '\n--------END RESPONSE--------'
                % etree.tostring(response, pretty_print=True)
            )
        except PyUPSException, e:
            if silent:
                return []

            error = e[0].split(':')
            if error[0] in ['Hard-111285', 'Hard-111286']:
                # Can't sit quite !
                # Hard-111285: The postal code %postal% is invalid for %state%
                #   %country%.
                # Hard-111286: %state% is not a valid state abbreviation for
                #   %country%.
                self.raise_user_error('InvalidAddress: %s' % unicode(error[1]))
            elif error[0] in ['Hard-111035', 'Hard-111036']:
                # Can't sit quite !
                # Hard-111035: The maximum per package weight for that service
                #   from the selected country is %country.maxPkgWeight% pounds.
                # Hard-111036: The maximum per package weight for that service
                #   from the selected country is %country.maxPkgWeight% kg.
                self.raise_user_error('WeightExceed: %s' % unicode(error[1]))
            self.raise_user_error(unicode(e[0]))

        rates = []
        for rated_shipment in response.iterchildren(tag='RatedShipment'):
            for service in carrier.services:
                if service.code == str(rated_shipment.Service.Code.text):
                    break
            else:
                continue

            currency, = Currency.search([
                ('code', '=', str(rated_shipment.TotalCharges.CurrencyCode))
            ])
            is_negotiated = False
            negotiated_rate = None
            original_cost = rated_shipment.TotalCharges.MonetaryValue
            if carrier.ups_negotiated_rates and \
                    hasattr(rated_shipment, 'NegotiatedRates'):
                # If there are negotiated rates return that instead
                negotiated_rate = rated_shipment.NegotiatedRates.NetSummaryCharges.GrandTotal.MonetaryValue  # noqa
                is_negotiated = True

            cost = currency.round(Decimal(
                str(negotiated_rate if is_negotiated else original_cost)
            ))

            rate = {
                'carrier_service': service,
                'cost': cost,
                'cost_currency': currency,
                'carrier': carrier,
                'ups_is_negotiated': is_negotiated,
                'ups_negotiated_rate': negotiated_rate,
                'ups_original_cost': original_cost,
            }

            if hasattr(rated_shipment, 'ScheduledDeliveryTime'):
                rate['ScheduledDeliveryTime'] = \
                    rated_shipment.ScheduledDeliveryTime.pyval
            if hasattr(rated_shipment, 'GuaranteedDaysToDelivery'):
                rate['GuaranteedDaysToDelivery'] = \
                    rated_shipment.GuaranteedDaysToDelivery.pyval

            duration = "%s" % (
                rate.get('GuaranteedDaysToDelivery') or rate.get('ScheduledDeliveryTime') or ''  # noqa
            )
            display_name = "UPS %s %s" % (
                service.name,
                "(%s business days)" % duration if duration else ''
            )
            rate['display_name'] = display_name

            rates.append(rate)
        return rates

    def _get_rate_request_xml(self, carrier, carrier_service):
        SaleConfiguration = Pool().get("sale.configuration")
        Uom = Pool().get('product.uom')
        config = SaleConfiguration(1)

        package_type = RatingService.packaging_type(
            Code=config.ups_box_type and config.ups_box_type.code
        )

        package_weight = RatingService.package_weight_type(
            Weight="%.2f" % Uom.compute_qty(
                self.weight_uom, self.weight, carrier.ups_weight_uom
            ),
            Code=carrier.ups_weight_uom_code,
        )
        package_service_options = RatingService.package_service_options_type(
            RatingService.insured_value_type(MonetaryValue='0')
        )
        package_container = RatingService.package_type(
            package_type,
            package_weight,
            package_service_options
        )
        shipment_args = [package_container]

        from_address = self._get_ship_from_address()

        shipment_args.extend([
            from_address.to_ups_shipper(carrier=carrier),  # Shipper
            self.shipment_address.to_ups_to_address(),      # Ship to
            from_address.to_ups_from_address(),   # Ship from

        ])

        if carrier.ups_negotiated_rates:
            shipment_args.append(
                RatingService.rate_information_type(negotiated=True)
            )

        if carrier_service:
            # TODO: handle ups_saturday_delivery
            shipment_args.append(
                RatingService.service_type(Code=carrier_service.code)
            )
            request_option = E.RequestOption('Rate')
        else:
            request_option = E.RequestOption('Shop')

        return RatingService.rating_request_type(
            E.Shipment(*shipment_args), RequestOption=request_option
        )

    def create_shipment(self, shipment_type):
        Shipment = Pool().get('stock.shipment.out')

        shipments = super(Sale, self).create_shipment(shipment_type)

        if shipment_type == 'out' and shipments:
            if self.carrier_cost_method == "ups":
                Shipment.write(list(shipments), {
                    'ups_saturday_delivery': self.ups_saturday_delivery,
                })
        return shipments
