# -*- coding: utf-8 -*-
"""
    carrier

"""
from trytond.model import fields
from trytond.pool import PoolMeta, Pool
from trytond.pyson import Eval
from ups.shipping_package import ShipmentConfirm, ShipmentAccept, ShipmentVoid
from ups.rating_package import RatingService
from ups.address_validation import AddressValidation

__all__ = ['Carrier', 'CarrierService', 'BoxType']
__metaclass__ = PoolMeta


class Carrier:
    "Carrier"
    __name__ = 'carrier'

    # UPS Configuration
    ups_license_key = fields.Char(
        'UPS License Key',
        states={
            'required': Eval('carrier_cost_method') == 'ups',
            'readonly': Eval('carrier_cost_method') != 'ups',
            'invisible': Eval('carrier_cost_method') != 'ups',
        },
        depends=['carrier_cost_method']
    )
    ups_user_id = fields.Char(
        'UPS User Id',
        states={
            'required': Eval('carrier_cost_method') == 'ups',
            'readonly': Eval('carrier_cost_method') != 'ups',
            'invisible': Eval('carrier_cost_method') != 'ups',
        },
        depends=['carrier_cost_method']
    )
    ups_password = fields.Char(
        'UPS User Password',
        states={
            'required': Eval('carrier_cost_method') == 'ups',
            'readonly': Eval('carrier_cost_method') != 'ups',
            'invisible': Eval('carrier_cost_method') != 'ups',
        },
        depends=['carrier_cost_method']
    )
    ups_shipper_no = fields.Char(
        'UPS Shipper Number',
        states={
            'required': Eval('carrier_cost_method') == 'ups',
            'readonly': Eval('carrier_cost_method') != 'ups',
            'invisible': Eval('carrier_cost_method') != 'ups',
        },
        depends=['carrier_cost_method']
    )
    ups_is_test = fields.Boolean(
        'Is Test',
        states={
            'readonly': Eval('carrier_cost_method') != 'ups',
            'invisible': Eval('carrier_cost_method') != 'ups',
        },
        depends=['carrier_cost_method']
    )
    ups_negotiated_rates = fields.Boolean(
        'Use negotiated rates',
        states={
            'readonly': Eval('carrier_cost_method') != 'ups',
            'invisible': Eval('carrier_cost_method') != 'ups',
        },
        depends=['carrier_cost_method']
    )
    ups_uom_system = fields.Selection([
        ('00', 'Metric Units Of Measurement'),
        ('01', 'English Units Of Measurement'),
    ], 'UOM System', states={
        'required': Eval('carrier_cost_method') == 'ups',
        'readonly': Eval('carrier_cost_method') != 'ups',
        'invisible': Eval('carrier_cost_method') != 'ups',
    }, depends=['carrier_cost_method'])
    ups_weight_uom = fields.Function(
        fields.Many2One(
            'product.uom', 'Weight UOM',
            states={
                'invisible': Eval('carrier_cost_method') != 'ups',
            },
            depends=['carrier_cost_method']
        ),
        'get_ups_default_uom'
    )
    ups_weight_uom_code = fields.Function(
        fields.Char(
            'Weight UOM code',
            states={
                'invisible': Eval('carrier_cost_method') != 'ups',
            },
            depends=['carrier_cost_method']
        ), 'get_ups_uom_code'
    )
    ups_length_uom = fields.Function(
        fields.Many2One(
            'product.uom', 'Length UOM',
            states={
                'invisible': Eval('carrier_cost_method') != 'ups',
            },
            depends=['carrier_cost_method']
        ),
        'get_ups_default_uom'
    )

    @classmethod
    def __setup__(cls):
        super(Carrier, cls).__setup__()

        for selection in [
                ('ups', 'UPS (Direct)'),
                ('ups_worldship', 'UPS Worldship (Direct)')
        ]:
            if selection not in cls.carrier_cost_method.selection:
                cls.carrier_cost_method.selection.append(selection)

        cls._error_messages.update({
            'ups_credentials_required':
                'UPS settings on UPS configuration are incomplete.',
        })

    def _get_ups_service_name(self, service):
        """
        Return display name for ups service

        This method can be overridden by downstream module to change the default
        display name of service
        """
        return "%s %s" % (
            self.carrier_product.code, service.name
        )

    @staticmethod
    def default_ups_uom_system():
        return '01'

    def get_ups_default_uom(self, name):
        """
        Return default UOM on basis of uom_system
        """
        UOM = Pool().get('product.uom')

        uom_map = {
            '00': {  # Metric
                'weight': 'kg',
                'length': 'cm',
            },
            '01': {  # English
                'weight': 'lb',
                'length': 'in',
            }
        }

        return UOM.search([
            ('symbol', '=', uom_map[self.ups_uom_system][name[4:-4]])
        ])[0].id

    def get_ups_uom_code(self, name):
        """
        Return UOM code names depending on the system
        """
        uom_map = {
            '00': {  # Metric
                'weight_uom_code': 'KGS',
                'length_uom_code': 'cm',
            },
            '01': {  # English
                'weight_uom_code': 'LBS',
                'length_uom_code': 'in',
            }
        }

        return uom_map[self.ups_uom_system][name[4:]]

    def ups_api_instance(self, call='confirm', return_xml=False):
        """Return Instance of UPS
        """
        if not all([
            self.ups_license_key,
            self.ups_user_id,
            self.ups_password,
            self.ups_uom_system,
        ]):
            self.raise_user_error('ups_credentials_required')

        if call == 'confirm':
            call_method = ShipmentConfirm
        elif call == 'accept':
            call_method = ShipmentAccept
        elif call == 'void':
            call_method = ShipmentVoid
        elif call == 'rate':
            call_method = RatingService
        elif call == 'address_val':
            call_method = AddressValidation
        else:
            call_method = None

        if call_method:
            return call_method(
                license_no=self.ups_license_key,
                user_id=self.ups_user_id,
                password=self.ups_password,
                sandbox=self.ups_is_test,
                return_xml=return_xml
            )


class CarrierService:
    __name__ = 'carrier.service'

    @classmethod
    def __setup__(cls):
        super(CarrierService, cls).__setup__()

        for selection in [
                ('ups', 'UPS (Direct)'),
                ('ups_worldship', 'UPS Worldship (Direct)')
        ]:
            if selection not in cls.carrier_cost_method.selection:
                cls.carrier_cost_method.selection.append(selection)


class BoxType:
    __name__ = "carrier.box_type"

    @classmethod
    def __setup__(cls):
        super(BoxType, cls).__setup__()

        for selection in [
                ('ups', 'UPS (Direct)'),
                ('ups_worldship', 'UPS Worldship (Direct)')
        ]:
            if selection not in cls.carrier_cost_method.selection:
                cls.carrier_cost_method.selection.append(selection)
