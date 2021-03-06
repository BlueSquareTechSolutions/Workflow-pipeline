import logging
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import graphene
import pytest
from django.core.exceptions import ValidationError

from ......checkout import calculations
from ......checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ......order import OrderEvents, OrderStatus
from ......plugins.manager import get_plugins_manager
from ..... import ChargeStatus, TransactionKind
from .....utils import price_to_minor_unit
from ...webhooks import (
    confirm_payment_and_set_back_to_confirm,
    create_new_transaction,
    handle_authorization,
    handle_cancel_or_refund,
    handle_cancellation,
    handle_capture,
    handle_failed_capture,
    handle_failed_refund,
    handle_not_created_order,
    handle_pending,
    handle_refund,
    handle_reversed_refund,
    webhook_not_implemented,
)

logger = logging.getLogger(__name__)


def test_handle_authorization_for_order(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    handle_authorization(notification, config)

    assert payment.transactions.count() == 2
    transaction = payment.transactions.last()
    assert transaction.is_success is True
    assert transaction.kind == TransactionKind.AUTH
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_authorization_for_order_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_authorization(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_multiple_authorization_notification(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.NOT_CHARGED
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    first_notification = notification(
        merchant_reference=payment_id,
        success="false",
        psp_reference="wrong",
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin(adyen_auto_capture=True).config
    handle_authorization(first_notification, config)

    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.NOT_CHARGED
    capture_transaction = payment.transactions.get(kind=TransactionKind.CAPTURE)
    assert capture_transaction.is_success is False
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1

    second_notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    handle_authorization(second_notification, config)
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    assert payment.captured_amount == payment.total
    capture_transaction = payment.transactions.filter(
        kind=TransactionKind.CAPTURE
    ).last()
    assert capture_transaction.is_success is True
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 2


def test_handle_authorization_for_pending_order(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.PENDING
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin(adyen_auto_capture=True).config
    handle_authorization(notification, config)

    assert payment.transactions.count() == 2
    transaction = payment.transactions.last()
    assert transaction.is_success is True
    assert transaction.kind == TransactionKind.CAPTURE
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_authorization_for_checkout(
    notification,
    adyen_plugin,
    payment_adyen_for_checkout,
    address,
    shipping_method,
):
    checkout = payment_adyen_for_checkout.checkout
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.billing_address = address
    checkout.save()
    checkout_token = str(checkout.token)

    payment = payment_adyen_for_checkout
    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, checkout_info, lines, address
    )
    payment.is_active = True
    payment.order = None
    payment.total = total.gross.amount
    payment.currency = total.gross.currency
    payment.to_confirm = True
    payment.save()

    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    handle_authorization(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == 2
    transaction = payment.transactions.exclude(
        kind=TransactionKind.ACTION_TO_CONFIRM
    ).get()
    assert transaction.is_success is True
    assert transaction.kind == TransactionKind.AUTH
    assert payment.checkout is None
    assert payment.order
    assert payment.order.checkout_token == checkout_token
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


@patch("saleor.payment.gateway.void")
def test_handle_authorization_for_checkout_one_of_variants_deleted(
    void_mock,
    notification,
    adyen_plugin,
    payment_adyen_for_checkout,
    address,
    shipping_method,
):
    checkout = payment_adyen_for_checkout.checkout
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.billing_address = address
    checkout.save()

    payment = payment_adyen_for_checkout
    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, checkout_info, lines, address
    )
    payment.is_active = True
    payment.order = None
    payment.total = total.gross.amount
    payment.currency = total.gross.currency
    payment.to_confirm = True
    payment.save()

    checkout.lines.first().delete()

    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    handle_authorization(notification, config)

    payment.refresh_from_db()
    assert void_mock.call_count == 1
    assert not payment.order
    assert payment.checkout
    assert payment.transactions.count() == 2
    transaction = payment.transactions.exclude(
        kind=TransactionKind.ACTION_TO_CONFIRM
    ).get()
    assert transaction.is_success is True
    assert transaction.kind == TransactionKind.AUTH


def test_handle_authorization_with_adyen_auto_capture(
    notification, adyen_plugin, payment_adyen_for_checkout, address, shipping_method
):
    checkout = payment_adyen_for_checkout.checkout
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.billing_address = address
    checkout.save()

    payment = payment_adyen_for_checkout
    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, checkout_info, lines, address
    )
    payment.is_active = True
    payment.order = None
    payment.total = total.gross.amount
    payment.currency = total.gross.currency
    payment.to_confirm = True
    payment.save()

    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )

    plugin = adyen_plugin(adyen_auto_capture=True)
    handle_authorization(notification, plugin.config)

    payment.refresh_from_db()
    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.CAPTURE).get()
    assert transaction.is_success is True
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


@pytest.mark.vcr
def test_handle_authorization_with_auto_capture(
    notification, adyen_plugin, payment_adyen_for_checkout
):
    payment = payment_adyen_for_checkout
    payment.to_confirm = True
    payment.save()

    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        psp_reference="853596537720508F",
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin(adyen_auto_capture=False, auto_capture=True).config

    handle_authorization(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.CAPTURE).get()
    assert transaction.is_success is True
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_authorization_with_adyen_auto_capture_and_payment_charged(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    config.connection_params["adyen_auto_capture"] = True
    handle_authorization(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == 2
    assert payment.transactions.filter(kind=TransactionKind.CAPTURE).exists()
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


@pytest.mark.parametrize("payment_is_active", (True, False))
def test_handle_cancel(
    payment_is_active, notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.is_active = payment_is_active
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    handle_cancellation(notification, config)

    payment.order.refresh_from_db()
    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.CANCEL).get()
    assert transaction.is_success is True

    assert payment.order.status == OrderStatus.CANCELED
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_cancel_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_cancellation(notification, config)

    payment.order.refresh_from_db()
    assert payment.transactions.count() == transaction_count

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_cancel_already_canceled(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    create_new_transaction(notification, payment, TransactionKind.CANCEL)

    handle_cancellation(notification, config)

    assert payment.transactions.count() == 2


def test_handle_capture_for_order(notification, adyen_plugin, payment_adyen_for_order):
    payment = payment_adyen_for_order
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    handle_capture(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.CAPTURE).get()
    assert transaction.is_success is True
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_capture_for_checkout(
    notification,
    adyen_plugin,
    payment_adyen_for_checkout,
    address,
    shipping_method,
):
    checkout = payment_adyen_for_checkout.checkout
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.billing_address = address
    checkout.save()
    checkout_token = str(checkout.token)

    payment = payment_adyen_for_checkout
    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, checkout_info, lines, address
    )
    payment.is_active = True
    payment.order = None
    payment.total = total.gross.amount
    payment.currency = total.gross.currency
    payment.to_confirm = True
    payment.save()

    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    handle_capture(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == 2
    transaction = payment.transactions.exclude(
        kind=TransactionKind.ACTION_TO_CONFIRM
    ).get()
    assert transaction.is_success is True
    assert transaction.kind == TransactionKind.AUTH
    assert payment.checkout is None
    assert payment.order
    assert payment.order.checkout_token == checkout_token
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


@patch("saleor.payment.gateway.void")
def test_handle_capture_for_checkout_order_not_created_checkout_line_variant_deleted(
    void_mock,
    notification,
    adyen_plugin,
    payment_adyen_for_checkout,
    address,
    shipping_method,
):
    """
    Ensure that payment is not captured when one of checkout line variant is deleted.
    """

    # given
    checkout = payment_adyen_for_checkout.checkout
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.billing_address = address
    checkout.save()

    payment = payment_adyen_for_checkout
    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, checkout_info, lines, address
    )
    payment.is_active = True
    payment.order = None
    payment.total = total.gross.amount
    payment.currency = total.gross.currency
    payment.to_confirm = True
    payment.save()

    checkout.lines.first().delete()

    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    # when
    handle_capture(notification, config)

    # then
    payment.refresh_from_db()
    assert void_mock.call_count == 1
    assert not payment.order
    assert payment.checkout

    assert payment.transactions.count() == 2
    transaction = payment.transactions.exclude(
        kind=TransactionKind.ACTION_TO_CONFIRM
    ).get()
    assert transaction.is_success is True
    assert transaction.kind == TransactionKind.AUTH


def test_handle_capture_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_capture(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_capture_with_payment_already_charged(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    handle_capture(notification, config)

    # Payment is already captured so no need to save capture transaction
    payment.refresh_from_db()
    assert payment.transactions.count() == 2
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


@pytest.mark.parametrize(
    "charge_status", [ChargeStatus.NOT_CHARGED, ChargeStatus.FULLY_CHARGED]
)
def test_handle_failed_capture(
    charge_status, notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = charge_status
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    handle_failed_capture(notification, config)

    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.CAPTURE_FAILED).get()
    assert transaction.is_success is True
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.NOT_CHARGED
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_failed_capture_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_failed_capture(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_failed_capture_partial_charge(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount += payment.total * 2
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    handle_failed_capture(notification, config)

    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.CAPTURE_FAILED).get()
    assert transaction.is_success is True
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.PARTIALLY_CHARGED
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_pending(notification, adyen_plugin, payment_adyen_for_order):
    payment = payment_adyen_for_order
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    handle_pending(notification, config)

    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.PENDING).get()
    assert transaction.is_success is True
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.PENDING
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_pending_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_pending(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_pending_with_adyen_auto_capture(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    config.connection_params["adyen_auto_capture"] = True

    handle_pending(notification, config)

    # in case of autocapture we don't want to store the pending status as all payments
    # by default get capture status.
    assert payment.transactions.count() == 2
    assert payment.transactions.filter(kind=TransactionKind.PENDING).first()
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.PENDING
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_pending_already_pending(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.PENDING
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    create_new_transaction(notification, payment, TransactionKind.PENDING)

    handle_pending(notification, config)

    assert payment.transactions.filter(kind=TransactionKind.PENDING).exists()


@mock.patch("saleor.payment.gateways.adyen.webhooks.order_refunded")
def test_handle_refund(
    mock_order_refunded, notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    handle_refund(notification, config)

    assert payment.transactions.count() == 2
    transaction = payment.transactions.filter(kind=TransactionKind.REFUND).get()
    assert transaction.is_success is True
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.FULLY_REFUNDED
    assert payment.captured_amount == Decimal("0.00")

    mock_order_refunded.assert_called_once_with(
        payment.order, None, None, transaction.amount, payment, mock.ANY
    )
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_refund_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_refund(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


@mock.patch("saleor.payment.gateways.adyen.webhooks.order_refunded")
def test_handle_refund_already_refunded(
    mock_order_refunded, notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_REFUNDED
    payment.captured_amount = Decimal("0.00")
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    create_new_transaction(notification, payment, TransactionKind.REFUND)
    config = adyen_plugin().config

    handle_refund(notification, config)

    assert payment.transactions.count() == 2  # AUTH, REFUND
    assert not mock_order_refunded.called


def test_handle_failed_refund_missing_transaction(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    handle_failed_refund(notification, config)

    assert payment.transactions.count() == 1
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_failed_refund_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_failed_refund(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_failed_refund_with_transaction_refund_ongoing(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    create_new_transaction(notification, payment, TransactionKind.REFUND_ONGOING)
    handle_failed_refund(notification, config)

    # ACTION_TO_CONFIRM, REFUND_ONGOING, REFUND_FAILED, FULLY_CHARGED
    assert payment.transactions.count() == 4
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    assert payment.total == payment.captured_amount
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_failed_refund_with_transaction_refund(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_REFUNDED
    payment.captured_amount = Decimal("0.0")
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    create_new_transaction(notification, payment, TransactionKind.REFUND)
    handle_failed_refund(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == 4  # REFUND, REFUND_FAILED, FULLY_CHARGED
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    assert payment.total == payment.captured_amount
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_reversed_refund(notification, adyen_plugin, payment_adyen_for_order):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_REFUNDED
    payment.captured_amount = Decimal("0.0")
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    handle_reversed_refund(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.filter(kind=TransactionKind.REFUND_REVERSED).exists()
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    assert payment.total == payment.captured_amount
    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_handle_reversed_refund_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_REFUNDED
    payment.captured_amount = Decimal("0.0")
    payment.save()
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    handle_reversed_refund(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_reversed_refund_already_processed(
    notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    create_new_transaction(notification, payment, TransactionKind.REFUND_REVERSED)
    handle_reversed_refund(notification, config)

    assert payment.transactions.filter(kind=TransactionKind.REFUND_REVERSED).exists()


def test_webhook_not_implemented(notification, adyen_plugin, payment_adyen_for_order):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config

    webhook_not_implemented(notification, config)

    external_events = payment.order.events.filter(
        type=OrderEvents.EXTERNAL_SERVICE_NOTIFICATION
    )
    assert external_events.count() == 1


def test_webhook_not_implemented_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    config = adyen_plugin().config
    transaction_count = payment.transactions.count()

    caplog.set_level(logging.WARNING)

    webhook_not_implemented(notification, config)

    payment.refresh_from_db()
    assert payment.transactions.count() == transaction_count
    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


@mock.patch("saleor.payment.gateways.adyen.webhooks.handle_refund")
def test_handle_cancel_or_refund_action_refund(
    mock_handle_refund, notification, adyen_plugin, payment_adyen_for_order
):

    payment = payment_adyen_for_order
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    config = adyen_plugin().config
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    notification["additionalData"]["modification.action"] = "refund"

    handle_cancel_or_refund(notification, config)

    mock_handle_refund.assert_called_once_with(notification, config)


@mock.patch("saleor.payment.gateways.adyen.webhooks.handle_cancellation")
def test_handle_cancel_or_refund_action_cancel(
    mock_handle_cancellation, notification, adyen_plugin, payment_adyen_for_order
):
    payment = payment_adyen_for_order
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    config = adyen_plugin().config
    notification = notification(
        merchant_reference=payment_id,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    notification["additionalData"]["modification.action"] = "cancel"

    handle_cancel_or_refund(notification, config)

    mock_handle_cancellation.assert_called_once_with(notification, config)


def test_handle_cancel_or_refund_action_cancel_invalid_payment_id(
    notification, adyen_plugin, payment_adyen_for_order, caplog
):
    payment = payment_adyen_for_order
    config = adyen_plugin().config
    invalid_reference = "test invalid reference"
    notification = notification(
        merchant_reference=invalid_reference,
        value=price_to_minor_unit(payment.total, payment.currency),
    )
    notification["additionalData"]["modification.action"] = "cancel"

    caplog.set_level(logging.WARNING)

    handle_cancel_or_refund(notification, config)

    assert f"Unable to decode the payment ID {invalid_reference}." in caplog.text


def test_handle_not_created_order_order_created(
    checkout_ready_to_complete, payment_adyen_for_checkout, adyen_plugin, notification
):
    payment_adyen_for_checkout.charge_status = ChargeStatus.FULLY_CHARGED
    payment_adyen_for_checkout.save(update_fields=["charge_status"])

    adyen_plugin()
    handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )

    payment_adyen_for_checkout.refresh_from_db()

    assert payment_adyen_for_checkout.order


@patch("saleor.payment.gateway.refund")
def test_handle_not_created_order_order_not_created_checkout_line_variant_deleted(
    refund_mock,
    checkout_ready_to_complete,
    payment_adyen_for_checkout,
    adyen_plugin,
    notification,
):
    """Validate if order is not created when one of checkout line variant is deleted."""

    # given
    checkout = payment_adyen_for_checkout.checkout
    checkout.lines.first().variant.delete()

    payment_adyen_for_checkout.charge_status = ChargeStatus.FULLY_CHARGED
    payment_adyen_for_checkout.captured_amount = payment_adyen_for_checkout.total
    payment_adyen_for_checkout.save(update_fields=["charge_status", "captured_amount"])

    adyen_plugin()

    # when
    handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )

    # then
    assert refund_mock.call_count == 1
    payment_adyen_for_checkout.refresh_from_db()
    assert not payment_adyen_for_checkout.order


@patch("saleor.payment.gateway.refund")
@patch("saleor.checkout.complete_checkout._get_order_data")
def test_handle_not_created_order_refund_when_create_order_raises(
    order_data_mock, refund_mock, payment_adyen_for_checkout, adyen_plugin, notification
):
    order_data_mock.side_effect = ValidationError("Test error")

    payment_adyen_for_checkout.charge_status = ChargeStatus.FULLY_CHARGED
    payment_adyen_for_checkout.save(update_fields=["charge_status"])

    adyen_plugin()
    handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )

    assert payment_adyen_for_checkout.can_refund()
    assert refund_mock.call_count == 1


@patch("saleor.payment.gateway.void")
@patch("saleor.checkout.complete_checkout._get_order_data")
def test_handle_not_created_order_void_when_create_order_raises(
    order_data_mock, void_mock, payment_adyen_for_checkout, adyen_plugin, notification
):
    order_data_mock.side_effect = ValidationError("Test error")

    payment_adyen_for_checkout.charge_status = ChargeStatus.NOT_CHARGED
    payment_adyen_for_checkout.save(update_fields=["charge_status"])

    adyen_plugin()
    handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )

    assert payment_adyen_for_checkout.can_void()
    assert void_mock.call_count == 1


@pytest.mark.parametrize(
    "charge_status",
    [
        ChargeStatus.PARTIALLY_REFUNDED,
        ChargeStatus.REFUSED,
        ChargeStatus.FULLY_REFUNDED,
        ChargeStatus.CANCELLED,
    ],
)
def test_handle_not_created_order_return_none(
    charge_status, payment_adyen_for_checkout, adyen_plugin, notification
):
    payment_adyen_for_checkout.charge_status = charge_status
    payment_adyen_for_checkout.save(update_fields=["charge_status"])

    assert not handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )


def test_handle_not_created_order_create_new_success_transaction(
    payment_adyen_for_checkout, adyen_plugin, notification
):
    payment_adyen_for_checkout.charge_status = ChargeStatus.NOT_CHARGED
    payment_adyen_for_checkout.save(update_fields=["charge_status"])
    payment_adyen_for_checkout.transactions.all().delete()

    adyen_plugin()
    handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )

    payment_adyen_for_checkout.refresh_from_db()
    assert payment_adyen_for_checkout.order

    all_payment_transactions = payment_adyen_for_checkout.transactions.all()
    assert len(all_payment_transactions) == 2
    assert all_payment_transactions[0].kind == TransactionKind.ACTION_TO_CONFIRM
    assert all_payment_transactions[1].kind == TransactionKind.AUTH


@patch("saleor.payment.gateway.refund")
@patch("saleor.checkout.complete_checkout._get_order_data")
def test_handle_not_created_order_success_transaction_create_order_raises_and_refund(
    order_data_mock, refund_mock, payment_adyen_for_checkout, adyen_plugin, notification
):
    order_data_mock.side_effect = ValidationError("Test error")

    payment_adyen_for_checkout.charge_status = ChargeStatus.FULLY_CHARGED
    payment_adyen_for_checkout.save(update_fields=["charge_status"])
    payment_adyen_for_checkout.transactions.all().delete()

    adyen_plugin()
    handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )

    payment_adyen_for_checkout.refresh_from_db()
    assert not payment_adyen_for_checkout.order

    all_payment_transactions = payment_adyen_for_checkout.transactions.all()
    assert len(all_payment_transactions) == 2
    assert all_payment_transactions[0].kind == TransactionKind.ACTION_TO_CONFIRM
    assert all_payment_transactions[1].kind == TransactionKind.AUTH

    assert payment_adyen_for_checkout.can_refund()
    assert refund_mock.call_count == 1


@patch("saleor.payment.gateway.void")
@patch("saleor.checkout.complete_checkout._get_order_data")
def test_handle_not_created_order_success_transaction_create_order_raises_and_void(
    order_data_mock, void_mock, payment_adyen_for_checkout, adyen_plugin, notification
):
    order_data_mock.side_effect = ValidationError("Test error")

    payment_adyen_for_checkout.charge_status = ChargeStatus.NOT_CHARGED
    payment_adyen_for_checkout.save(update_fields=["charge_status"])
    payment_adyen_for_checkout.transactions.all().delete()

    adyen_plugin()
    handle_not_created_order(
        notification(),
        payment_adyen_for_checkout,
        payment_adyen_for_checkout.checkout,
        TransactionKind.CAPTURE,
        get_plugins_manager(),
    )

    payment_adyen_for_checkout.refresh_from_db()
    assert not payment_adyen_for_checkout.order

    all_payment_transactions = payment_adyen_for_checkout.transactions.all()
    assert len(all_payment_transactions) == 2
    assert all_payment_transactions[0].kind == TransactionKind.ACTION_TO_CONFIRM
    assert all_payment_transactions[1].kind == TransactionKind.AUTH

    assert payment_adyen_for_checkout.can_void()
    assert void_mock.call_count == 1


def test_confirm_payment_and_set_back_to_confirm(
    payment_adyen_for_checkout, adyen_plugin, notification
):
    plugin = adyen_plugin()
    payment_adyen_for_checkout.to_confirm = True
    payment_adyen_for_checkout.save(update_fields=["to_confirm"])
    create_new_transaction(
        notification(), payment_adyen_for_checkout, TransactionKind.ACTION_TO_CONFIRM
    )

    confirm_payment_and_set_back_to_confirm(
        payment_adyen_for_checkout, get_plugins_manager(), plugin.channel.slug
    )

    payment_adyen_for_checkout.refresh_from_db()

    assert payment_adyen_for_checkout.to_confirm
