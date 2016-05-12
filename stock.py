# -*- coding: utf-8 -*-
"""
    stock.py

"""
from decimal import Decimal
import base64
from lxml import etree
from lxml.builder import E
from logbook import Logger

from ups.shipping_package import ShipmentConfirm, ShipmentAccept
from ups.rating_package import RatingService
from ups.base import PyUPSException
from ups.worldship_api import WorldShip
from trytond.model import fields, ModelView
from trytond.transaction import Transaction
from trytond.wizard import Wizard, StateView, Button
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval
from trytond.rpc import RPC

__metaclass__ = PoolMeta
__all__ = [
    'ShipmentOut', 'StockMove', 'ShippingUps',
    'GenerateShippingLabel', 'Package'
]

STATES = {
    'readonly': Eval('state') == 'done',
}
logger = Logger('trytond_ups')


class ShipmentOut:
    "Shipment Out"
    __name__ = 'stock.shipment.out'

    ups_saturday_delivery = fields.Boolean(
        "Is Saturday Delivery", states=STATES, depends=['state']
    )

    @staticmethod
    def default_ups_saturday_delivery():
        return False

    @classmethod
    def __setup__(cls):
        super(ShipmentOut, cls).__setup__()
        # There can be cases when people might want to use a different
        # shipment carrier at any state except `done`.
        cls.carrier.states = STATES
        cls._error_messages.update({
            'ups_wrong_carrier':
                'Carrier for selected shipment is not UPS',
            'carrier_service_missing':
                'UPS service type missing.',
            'tracking_number_already_present':
                'Tracking Number is already present for this shipment.',
            'invalid_state': 'Labels can only be generated when the '
                'shipment is in Packed or Done states only',
            'no_packages': 'Shipment %s has no packages',
        })
        cls.__rpc__.update({
            'make_ups_labels': RPC(readonly=False, instantiate=0),
            'get_ups_shipping_cost': RPC(readonly=False, instantiate=0),
            'get_worldship_xml': RPC(instantiate=0, readonly=True),
        })

    def _get_ups_packages(self):
        """
        Return UPS Packages XML
        """
        package_containers = []

        for package in self.packages:
            package_containers.append(package.get_ups_package_container())
        return package_containers

    def _get_carrier_context(self):
        "Pass shipment in the context"
        context = super(ShipmentOut, self)._get_carrier_context()

        if not self.carrier.carrier_cost_method == 'ups':
            return context

        context = context.copy()
        context['shipment'] = self.id
        return context

    def get_shipping_rate(self, carrier, carrier_service=None, silent=False):
        Currency = Pool().get('currency.currency')

        if carrier.carrier_cost_method != 'ups':
            return super(ShipmentOut, self).get_shipping_rate(
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
            if carrier.ups_negotiated_rates and \
                    hasattr(rated_shipment, 'NegotiatedRates'):
                # If there are negotiated rates return that instead
                cost = rated_shipment.NegotiatedRates.NetSummaryCharges.GrandTotal.MonetaryValue  # noqa
            else:
                cost = rated_shipment.TotalCharges.MonetaryValue

            cost = currency.round(Decimal(str(cost)))

            rate = {
                'carrier_service': service,
                'cost': cost,
                'cost_currency': currency,
                'carrier': carrier,
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
            display_name = "%s %s" % (
                carrier.rec_name,
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
                self.weight_uom, self.weight, self.carrier.ups_weight_uom
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
            self.delivery_address.to_ups_to_address(),      # Ship to
            from_address.to_ups_from_address(),   # Ship from

        ])

        if carrier.ups_negotiated_rates:
            shipment_args.append(
                RatingService.rate_information_type(negotiated=True)
            )

        if carrier_service:
            # TODO: handle ups_saturday_delivery
            shipment_args.append(
                RatingService.service_type(Code=self.carrier_service.code)
            )
            request_option = E.RequestOption('Rate')
        else:
            request_option = E.RequestOption('Shop')

        return RatingService.rating_request_type(
            E.Shipment(*shipment_args), RequestOption=request_option
        )

    def _get_ups_shipment_cost(self, shipment_confirm):
        """
        The shipment_confirm is an xml container in the response which has the
        standard rates and negotiated rates. This method should extract the
        value and return it with the currency
        """
        Currency = Pool().get('currency.currency')

        shipment_charges = shipment_confirm.ShipmentCharges

        currency, = Currency.search([
            ('code', '=', str(
                shipment_charges.TotalCharges.CurrencyCode
            ))
        ])

        if self.carrier.ups_negotiated_rates and \
                hasattr(shipment_confirm, 'NegotiatedRates'):
            # If there are negotiated rates return that instead
            charges = shipment_confirm.NegotiatedRates.NetSummaryCharges
            charges = currency.round(Decimal(
                str(charges.GrandTotal.MonetaryValue)
            ))
        else:
            charges = currency.round(
                Decimal(str(shipment_charges.TotalCharges.MonetaryValue))
            )
        return charges, currency

    def _get_shipment_confirm_xml(self):
        """
        Return XML of shipment for shipment_confirm
        """
        Company = Pool().get('company.company')

        carrier = self.carrier
        if not self.carrier_service:
            self.raise_user_error('ups_service_type_missing')

        payment_info_prepaid = \
            ShipmentConfirm.payment_information_prepaid_type(
                AccountNumber=carrier.ups_shipper_no
            )
        payment_info = ShipmentConfirm.payment_information_type(
            payment_info_prepaid)
        packages = self._get_ups_packages()
        shipment_service = ShipmentConfirm.shipment_service_option_type(
            SaturdayDelivery='1' if self.ups_saturday_delivery
            else 'None'
        )
        description = ','.join([
            move.product.name for move in self.outgoing_moves
        ])
        from_address = self._get_ship_from_address()

        shipment_args = [
            from_address.to_ups_shipper(carrier=carrier),
            self.delivery_address.to_ups_to_address(),
            from_address.to_ups_from_address(),
            ShipmentConfirm.service_type(Code=self.carrier_service.code),
            payment_info, shipment_service,
        ]
        if carrier.ups_negotiated_rates:
            shipment_args.append(
                ShipmentConfirm.rate_information_type(negotiated=True)
            )
        if from_address.country.code == 'US' and \
                self.delivery_address.country.code in ['PR', 'CA']:
            # Special case for US to PR or CA InvoiceLineTotal should be sent
            monetary_value = str(sum(map(
                lambda move: move.get_monetary_value_for_ups(),
                self.outgoing_moves
            )))

            company_id = Transaction().context.get('company')
            if not company_id:
                self.raise_user_error("Company is not in context")

            company = Company(company_id)
            shipment_args.append(ShipmentConfirm.invoice_line_total_type(
                MonetaryValue=monetary_value,
                CurrencyCode=company.currency.code
            ))

        shipment_args.extend(packages)
        shipment_confirm = ShipmentConfirm.shipment_confirm_request_type(
            *shipment_args, Description=description[:35]
        )
        return shipment_confirm

    def generate_shipping_labels(self, **kwargs):
        Attachment = Pool().get('ir.attachment')
        Tracking = Pool().get('shipment.tracking')

        if self.carrier_cost_method != "ups":
            return super(ShipmentOut, self).generate_shipping_labels(**kwargs)

        carrier = self.carrier
        if self.state not in ('packed', 'done'):
            self.raise_user_error('invalid_state')

        if self.carrier_cost_method != "ups":
            self.raise_user_error('ups_wrong_carrier')

        if self.tracking_number:
            self.raise_user_error('tracking_number_already_present')

        if not self.packages:
            self.raise_user_error("no_packages", error_args=(self.id,))

        shipment_confirm = self._get_shipment_confirm_xml()
        shipment_confirm_instance = carrier.ups_api_instance(call="confirm")

        # Logging.
        logger.debug(
            'Making Shipment Confirm Request for'
            'Shipment ID: {0} and Carrier ID: {1}'
            .format(self.id, self.carrier.id)
        )
        logger.debug(
            '--------SHIPMENT CONFIRM REQUEST--------\n%s'
            '\n--------END REQUEST--------'
            % etree.tostring(shipment_confirm, pretty_print=True)
        )

        try:
            response = shipment_confirm_instance.request(shipment_confirm)

            # Logging.
            logger.debug(
                '--------SHIPMENT CONFIRM RESPONSE--------\n%s'
                '\n--------END RESPONSE--------'
                % etree.tostring(response, pretty_print=True)
            )
        except PyUPSException, e:
            self.raise_user_error(unicode(e[0]))

        digest = ShipmentConfirm.extract_digest(response)

        shipment_accept = ShipmentAccept.shipment_accept_request_type(digest)

        shipment_accept_instance = carrier.ups_api_instance(call="accept")

        # Logging.
        logger.debug(
            'Making Shipment Accept Request for'
            'Shipment ID: {0} and Carrier ID: {1}'
            .format(self.id, self.carrier.id)
        )
        logger.debug(
            '--------SHIPMENT ACCEPT REQUEST--------\n%s'
            '\n--------END REQUEST--------'
            % etree.tostring(shipment_accept, pretty_print=True)
        )

        try:
            response = shipment_accept_instance.request(shipment_accept)

            # Logging.
            logger.debug(
                '--------SHIPMENT ACCEPT RESPONSE--------\n%s'
                '\n--------END RESPONSE--------'
                % etree.tostring(response, pretty_print=True)
            )
        except PyUPSException, e:
            self.raise_user_error(unicode(e[0]))

        shipment_res = response.ShipmentResults
        shipment_identification_number = \
            shipment_res.ShipmentIdentificationNumber.pyval

        shipping_cost, currency = self._get_ups_shipment_cost(shipment_res)

        self.__class__.write([self], {
            'cost': shipping_cost,
            'cost_currency': currency,
        })

        index = 0
        tracking_values = []
        for package in response.ShipmentResults.PackageResults:
            tracking_number = unicode(package.TrackingNumber.pyval)

            # The package results do not hold any info to identify which
            # result is for what package, instead it returns the results
            # in the order in which the packages were sent in request, so
            # we read the result in the same order.
            stock_package = self.packages[index]
            tracking_values.append({
                'carrier': self.carrier,
                'tracking_number': tracking_number,
                'origin': '%s,%d' % (
                    stock_package.__name__, stock_package.id
                )
            })

            index += 1

            data = stock_package._process_raw_label(
                package.LabelImage.GraphicImage.pyval
            )

            Attachment.create([{
                'name': "%s_%s_%s.png" % (
                    tracking_number,
                    shipment_identification_number,
                    stock_package.code,
                ),
                'data': buffer(base64.decodestring(data)),
                'resource': '%s,%s' % (self.__name__, self.id)
            }])

        Tracking.create(tracking_values)

        shipment_tracking_number, = Tracking.search([
            ('tracking_number', '=', shipment_identification_number)
        ])
        self.tracking_number = shipment_tracking_number.id
        self.save()

    def get_worldship_goods(self):
        """
        For all items in the shipment, this expects a manifest of Goods
        """
        goods = []
        for move in self.outgoing_moves:
            if not move.quantity:
                continue
            values = [
                E.PartNumber(move.product.code),
                E.DescriptionOfGood(move.product.name),
                E.InvoiceUnits(str(move.quantity)),
                E.InvoiceUnitOfMeasure(move.uom.symbol),
                E(
                    'Invoice-SED-UnitPrice',
                    str(move.unit_price.quantize(Decimal('0.1')))
                )
            ]
            if move.product.country_of_origin:
                values.append(
                    E(
                        'Inv-NAFTA-CO-CountryTerritoryOfOrigin',
                        move.product.country_of_origin.code
                    )
                )
            goods.append(E.Goods(*values))
        return goods

    def get_worldship_xml(self):
        """
        Return shipment data with worldship understandable xml
        """
        if not self.carrier:
            self.raise_user_error('Carrier is not defined for shipment.')
        if self.carrier.carrier_cost_method != 'ups_worldship':
            self.raise_user_error(
                'Shipment %s is to be shipped with %s, not Worldship.',
                (self.reference, self.carrier.rec_name)
            )

        description = ','.join([
            move.product.name for move in self.outgoing_moves
        ])
        ship_to = self.delivery_address.to_worldship_to_address()
        ship_from = self._get_ship_from_address().to_worldship_from_address()
        shipment_information = WorldShip.shipment_information_type(
            ServiceType="Standard",  # Worldease
            DescriptionOfGoods=description[:50],
            GoodsNotInFreeCirculation="0",
            BillTransportationTo="Shipper",
        )
        xml_packages = []
        for package in self.packages:
            xml_packages.append(WorldShip.package_type(
                PackageID=str(package.id),
                PackageType='CP',  # Custom Package
                Weight="%.2f" % package.weight,
            ))
        final_xml = WorldShip.get_xml(
            ship_to, ship_from, shipment_information,
            *(xml_packages + self.get_worldship_goods())
        )
        rv = {
            'id': self.id,
            'worldship_xml': final_xml,
        }
        return rv


class StockMove:
    "Stock move"
    __name__ = "stock.move"

    def get_monetary_value_for_ups(self):
        """
        Returns monetary_value as required for ups
        """
        ProductUom = Pool().get('product.uom')

        # Find the quantity in the default uom of the product as the weight
        # is for per unit in that uom
        if self.uom != self.product.default_uom:
            quantity = ProductUom.compute_qty(
                self.uom,
                self.quantity,
                self.product.default_uom
            )
        else:
            quantity = self.quantity

        return int(self.product.list_price * Decimal(quantity))


class ShippingUps(ModelView):
    'Generate Labels'
    __name__ = 'shipping.label.ups'

    ups_saturday_delivery = fields.Boolean("Is Saturday Delivery ?")


class GenerateShippingLabel(Wizard):
    'Generate Labels'
    __name__ = 'shipping.label'

    ups_config = StateView(
        'shipping.label.ups',
        'shipping_ups.shipping_ups_configuration_view_form',
        [
            Button('Back', 'start', 'tryton-go-previous'),
            Button('Continue', 'generate_labels', 'tryton-go-next'),
        ]
    )

    def default_ups_config(self, data):
        return {
            'ups_saturday_delivery': self.shipment.ups_saturday_delivery
        }

    def transition_next(self):
        state = super(GenerateShippingLabel, self).transition_next()

        if self.start.carrier.carrier_cost_method == 'ups':
            return 'ups_config'
        return state

    def transition_generate_labels(self):
        if self.start.carrier.carrier_cost_method == "ups":
            shipment = self.shipment
            shipment.ups_saturday_delivery = \
                self.ups_config.ups_saturday_delivery
            shipment.save()

        return super(GenerateShippingLabel, self).transition_generate_labels()


class Package:
    __name__ = 'stock.package'

    def get_ups_package_container(self):
        """
        Return UPS package container for a single package
        """
        shipment = self.shipment
        carrier = shipment.carrier

        package_type = ShipmentConfirm.packaging_type(
            Code=self.box_type.code
        )

        package_weight = ShipmentConfirm.package_weight_type(
            Weight="%.2f" % self.weight,
            Code=carrier.ups_weight_uom_code,
        )
        package_service_options = ShipmentConfirm.package_service_options_type(
            ShipmentConfirm.insured_value_type(MonetaryValue='0')
        )
        package_container = ShipmentConfirm.package_type(
            package_type,
            package_weight,
            package_service_options
        )
        return package_container
