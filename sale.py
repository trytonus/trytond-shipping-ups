# -*- coding: utf-8 -*-
"""
    sale.py

    :copyright: (c) 2014 by Openlabs Technologies & Consulting (P) Limited
    :license: BSD, see LICENSE for more details.
"""
from decimal import Decimal, ROUND_UP

from lxml import etree
from lxml.builder import E
from ups.rating_package import RatingService
from ups.base import PyUPSException
from trytond.model import ModelView, fields
from trytond.pool import PoolMeta, Pool
from trytond.transaction import Transaction
from trytond.pyson import Eval

__all__ = ['Configuration', 'Sale']
__metaclass__ = PoolMeta


UPS_PACKAGE_TYPES = [
    ('01', 'UPS Letter'),
    ('02', 'Customer Supplied Package'),
    ('03', 'Tube'),
    ('04', 'PAK'),
    ('21', 'UPS Express Box'),
    ('24', 'UPS 25KG Box'),
    ('25', 'UPS 10KG Box'),
    ('30', 'Pallet'),
    ('2a', 'Small Express Box'),
    ('2b', 'Medium Express Box'),
    ('2c', 'Large Express Box'),
]


class Configuration:
    'Sale Configuration'
    __name__ = 'sale.configuration'

    ups_service_type = fields.Many2One(
        'ups.service', 'Default UPS Service Type',
    )
    ups_package_type = fields.Selection(
        UPS_PACKAGE_TYPES, 'Package Content Type'
    )

    @staticmethod
    def default_ups_package_type():
        # This is the default value as specified in UPS doc
        return '02'


class Sale:
    "Sale"
    __name__ = 'sale.sale'

    is_ups_shipping = fields.Function(
        fields.Boolean('Is Shipping', readonly=True),
        'get_is_ups_shipping'
    )
    ups_service_type = fields.Many2One(
        'ups.service', 'UPS Service Type',
    )
    ups_package_type = fields.Selection(
        UPS_PACKAGE_TYPES, 'Package Content Type'
    )
    ups_saturday_delivery = fields.Boolean("Is Saturday Delivery")

    def _get_weight_uom(self):
        """
        Returns uom for ups
        """
        UPSConfiguration = Pool().get('ups.configuration')

        weight_uom = UPSConfiguration(1).weight_uom

        if self.is_ups_shipping and weight_uom:
            return weight_uom

        return super(Sale, self)._get_weight_uom()

    @classmethod
    def __setup__(cls):
        super(Sale, cls).__setup__()
        cls._buttons.update({
            'update_ups_shipment_cost': {
                'invisible': Eval('state') != 'quotation'
            }
        })

    @staticmethod
    def default_ups_package_type():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.ups_package_type

    @staticmethod
    def default_ups_service_type():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.ups_service_type and config.ups_service_type.id or None

    @staticmethod
    def default_ups_saturday_delivery():
        return False

    def on_change_lines(self):
        """Pass a flag in context which indicates the get_sale_price method
        of ups carrier not to calculate cost on each line change
        """
        with Transaction().set_context({'ignore_carrier_computation': True}):
            return super(Sale, self).on_change_lines()

    def get_is_ups_shipping(self, name):
        """
        Check if shipping is from UPS
        """
        return self.carrier and self.carrier.carrier_cost_method == 'ups'

    def _get_carrier_context(self):
        "Pass sale in the context"
        context = super(Sale, self)._get_carrier_context()

        if not self.carrier.carrier_cost_method == 'ups':
            return context

        context = context.copy()
        context['sale'] = self.id
        return context

    def apply_ups_shipping(self):
        "Add a shipping line to sale for ups"
        Currency = Pool().get('currency.currency')

        if self.is_ups_shipping:
            with Transaction().set_context(self._get_carrier_context()):
                shipment_cost, currency_id = self.carrier.get_sale_price()
                if not shipment_cost:
                    return
            # Convert the shipping cost to sale currency from USD
            shipment_cost = Currency.compute(
                Currency(currency_id), shipment_cost, self.currency
            )
            self.add_shipping_line(
                shipment_cost,
                "%s - %s" % (
                    self.carrier.party.name, self.ups_service_type.name
                )
            )

    @classmethod
    def quote(cls, sales):
        res = super(Sale, cls).quote(sales)
        cls.update_ups_shipment_cost(sales)
        return res

    @classmethod
    @ModelView.button
    def update_ups_shipment_cost(cls, sales):
        "Updates the shipping line with new value if any"
        for sale in sales:
            sale.apply_ups_shipping()

    def _update_ups_shipments(self):
        """
        Update shipments with ups data
        """
        Shipment = Pool().get('stock.shipment.out')

        assert self.is_ups_shipping

        shipments = list(self.shipments)
        Shipment.write(shipments, {
            'ups_service_type': self.ups_service_type.id,
            'ups_package_type': self.ups_package_type,
            'ups_saturday_delivery': self.ups_saturday_delivery,
        })

    def create_shipment(self, shipment_type):
        """
        Create shipments for sale
        """
        with Transaction().set_context(ignore_carrier_computation=True):
            # disable `carrier cost computation`(default behaviour) as cost
            # should only be computed after updating service_type else error may
            # occur, with improper ups service_type.
            shipments = super(Sale, self).create_shipment(shipment_type)

        if shipment_type == 'out' and shipments and self.is_ups_shipping:
            self._update_ups_shipments()
        return shipments

    def _get_ups_packages(self):
        """
        Return UPS Packages XML
        """
        UPSConfiguration = Pool().get('ups.configuration')

        ups_config = UPSConfiguration(1)

        package_type = RatingService.packaging_type(
            Code=self.ups_package_type
        )

        weight = self._get_package_weight(ups_config.weight_uom).quantize(
            Decimal('.01'), rounding=ROUND_UP
        )
        package_weight = RatingService.package_weight_type(
            Weight=str(weight),
            Code=ups_config.weight_uom_code,
        )
        package_service_options = RatingService.package_service_options_type(
            RatingService.insured_value_type(MonetaryValue='0')
        )
        package_container = RatingService.package_type(
            package_type,
            package_weight,
            package_service_options
        )
        return [package_container]

    def _get_ship_from_address(self):
        """
        Usually the warehouse from which you ship
        """
        return self.warehouse.address

    def _get_rate_request_xml(self, mode='rate'):
        """
        Return the E builder object with the rate fetching request

        :param mode: 'rate' - to fetch rate of current shipment and selected
                              package type
                     'shop' - to get a rates list
        """
        UPSConfiguration = Pool().get('ups.configuration')

        ups_config = UPSConfiguration(1)

        assert mode in ('rate', 'shop'), "Mode should be 'rate' or 'shop'"

        if mode == 'rate' and not self.ups_service_type:
            self.raise_user_error('ups_service_type_missing')

        shipment_args = self._get_ups_packages()

        shipment_args.extend([
            self.warehouse.address.to_ups_shipper(),        # Shipper
            self.shipment_address.to_ups_to_address(),      # Ship to
            self._get_ship_from_address().to_ups_from_address(),   # Ship from

        ])

        if ups_config.negotiated_rates:
            shipment_args.append(
                RatingService.rate_information_type(negotiated=True)
            )

        if mode == 'rate':
            # TODO: handle ups_saturday_delivery
            shipment_args.append(
                RatingService.service_type(Code=self.ups_service_type.code)
            )
            request_option = E.RequestOption('Rate')
        else:
            request_option = E.RequestOption('Shop')

        return RatingService.rating_request_type(
            E.Shipment(*shipment_args), RequestOption=request_option
        )

    def _get_ups_rate_from_rated_shipment(cls, rated_shipment):
        """
        The rated_shipment is an xml container in the response which has the
        standard rates and negotiated rates. This method should extract the
        value and return it with the currency
        """
        Currency = Pool().get('currency.currency')
        UPSConfiguration = Pool().get('ups.configuration')

        ups_config = UPSConfiguration(1)

        currency, = Currency.search([
            ('code', '=', str(rated_shipment.TotalCharges.CurrencyCode))
        ])
        if ups_config.negotiated_rates and \
                hasattr(rated_shipment, 'NegotiatedRates'):
            # If there are negotiated rates return that instead
            charges = rated_shipment.NegotiatedRates.NetSummaryCharges
            charges = currency.round(Decimal(
                str(charges.GrandTotal.MonetaryValue)
            ))
        else:
            charges = currency.round(
                Decimal(str(rated_shipment.TotalCharges.MonetaryValue))
            )
        return charges, currency

    def get_ups_shipping_cost(self):
        """Returns the calculated shipping cost as sent by ups

        :returns: The shipping cost with currency
        """
        UPSConfiguration = Pool().get('ups.configuration')
        Carrier = Pool().get('carrier')

        ups_config = UPSConfiguration(1)
        carrier, = Carrier.search(['carrier_cost_method', '=', 'ups'])

        rate_request = self._get_rate_request_xml()
        rate_api = ups_config.api_instance(call="rate")

        # Instead of shopping for rates, just get a price for the given
        # service and package type to the destination we know.
        rate_api.RequestOption = E.RequestOption('Rate')

        # Logging.
        ups_config.logger.debug(
            'Making Rate API Request for shipping cost of'
            'Sale ID: {0} and Carrier ID: {1}'
            .format(self.id, carrier.id)
        )
        ups_config.logger.debug(
            '--------RATE API REQUEST--------\n%s'
            '\n--------END REQUEST--------'
            % etree.tostring(rate_request, pretty_print=True)
        )

        try:
            response = rate_api.request(rate_request)

            # Logging.
            ups_config.logger.debug(
                '--------RATE API RESPONSE--------\n%s'
                '\n--------END RESPONSE--------'
                % etree.tostring(response, pretty_print=True)
            )
        except PyUPSException, e:
            self.raise_user_error(unicode(e[0]))

        shipment_cost, currency = self._get_ups_rate_from_rated_shipment(
            response.RatedShipment
        )
        return shipment_cost, currency.id

    def _ups_service_from_code(self, code):
        """
        Returns ups_service instance if code is allowed for this sale

        Downstream module can decide the eligibility of ups service for sale
        """
        UPSService = Pool().get('ups.service')

        try:
            service, = UPSService.search([
                ('code', '=', code)
            ])
        except ValueError:
            return None
        return service

    def _make_ups_rate_line(self, carrier, rated_shipment):
        """
        Build a rate line from the rated shipment
        """
        # First identify the service
        service = self._ups_service_from_code(
            str(rated_shipment.Service.Code.text)
        )
        if not service:
            return None

        cost, currency = self._get_ups_rate_from_rated_shipment(rated_shipment)

        # Extract metadata
        metadata = {}
        if hasattr(rated_shipment, 'ScheduledDeliveryTime'):
            metadata['ScheduledDeliveryTime'] = \
                rated_shipment.ScheduledDeliveryTime.pyval
        if hasattr(rated_shipment, 'GuaranteedDaysToDelivery'):
            metadata['GuaranteedDaysToDelivery'] = \
                rated_shipment.GuaranteedDaysToDelivery.pyval

        # values that need to be written back to sale order
        write_vals = {
            'carrier': carrier.id,
            'ups_service_type': service.id,
        }

        return (
            carrier._get_ups_service_name(service),
            cost,
            currency,
            metadata,
            write_vals,
        )

    def get_ups_shipping_rates(self, silent=True):
        """
        Call the rates service and get possible quotes for shipping the product
        """
        UPSConfiguration = Pool().get('ups.configuration')
        Carrier = Pool().get('carrier')

        ups_config = UPSConfiguration(1)
        carrier, = Carrier.search(['carrier_cost_method', '=', 'ups'])

        rate_request = self._get_rate_request_xml(mode='shop')
        rate_api = ups_config.api_instance(call="rate")

        # Logging.
        ups_config.logger.debug(
            'Making Rate API Request for shipping rates of'
            'Sale ID: {0} and Carrier ID: {1}'
            .format(self.id, carrier.id)
        )
        ups_config.logger.debug(
            '--------RATE API REQUEST--------\n%s'
            '\n--------END REQUEST--------'
            % etree.tostring(rate_request, pretty_print=True)
        )

        try:
            response = rate_api.request(rate_request)
            # Logging.
            ups_config.logger.debug(
                '--------START RATE API RESPONSE--------\n%s'
                '\n--------END RESPONSE--------'
                % etree.tostring(response, pretty_print=True)
            )
        except PyUPSException, e:
            error = e[0].split(':')
            if error[0] in ['Hard-111285', 'Hard-111286']:
                # Can't sit quite !
                # Hard-111285: The postal code %postal% is invalid for %state%
                #   %country%.
                # Hard-111286: %state% is not a valid state abbreviation for
                #   %country%.
                self.raise_user_error('InvalidAddress: %s' % unicode(error[1]))
            if error[0] in ['Hard-111035', 'Hard-111036']:
                # Can't sit quite !
                # Hard-111035: The maximum per package weight for that service
                #   from the selected country is %country.maxPkgWeight% pounds.
                # Hard-111036: The maximum per package weight for that service
                #   from the selected country is %country.maxPkgWeight% kg.
                self.raise_user_error('WeightExceed: %s' % unicode(error[1]))
            if silent:
                return []
            self.raise_user_error(unicode(e[0]))

        return filter(None, [
            self._make_ups_rate_line(carrier, rated_shipment)
            for rated_shipment in response.iterchildren(tag='RatedShipment')
        ])

    def on_change_carrier(self):
        """
        Show/Hide UPS Tab in view on change of carrier
        """
        res = super(Sale, self).on_change_carrier()

        res['is_ups_shipping'] = self.carrier and \
            self.carrier.carrier_cost_method == 'ups'

        return res
