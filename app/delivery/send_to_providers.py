from urllib import parse
from datetime import datetime

from flask import current_app
from notifications_utils.recipients import (
    validate_and_format_phone_number,
    validate_and_format_email_address
)
from notifications_utils.template import HTMLEmailTemplate, PlainTextEmailTemplate, SMSMessageTemplate

from app import clients, statsd_client, create_uuid
from app.dao.notifications_dao import (
    dao_update_notification,
    dao_get_notification_email_reply_for_notification,
    dao_get_notification_sms_sender_mapping)
from app.dao.provider_details_dao import (
    get_provider_details_by_notification_type,
    dao_toggle_sms_provider
)
from app.celery.research_mode_tasks import send_sms_response, send_email_response
from app.dao.templates_dao import dao_get_template_by_id
from app.models import (
    SMS_TYPE,
    KEY_TYPE_TEST,
    BRANDING_ORG,
    BRANDING_ORG_BANNER,
    BRANDING_GOVUK,
    EMAIL_TYPE,
    NOTIFICATION_CREATED,
    NOTIFICATION_TECHNICAL_FAILURE,
    NOTIFICATION_SENT,
    NOTIFICATION_SENDING
)
from app.celery.statistics_tasks import create_initial_notification_statistic_tasks


def send_sms_to_provider(notification):
    service = notification.service

    if not service.active:
        technical_failure(notification=notification)
        return

    if notification.status == 'created':
        provider = provider_to_use(SMS_TYPE, notification.id, notification.international)
        current_app.logger.info(
            "Starting sending SMS {} to provider at {}".format(notification.id, datetime.utcnow())
        )
        template_model = dao_get_template_by_id(notification.template_id, notification.template_version)

        sender_has_been_customised = (not service.get_prefix_sms_with_service_name())

        template = SMSMessageTemplate(
            template_model.__dict__,
            values=notification.personalisation,
            prefix=service.name,
            sender=sender_has_been_customised,
        )

        if service.research_mode or notification.key_type == KEY_TYPE_TEST:
            notification.billable_units = 0
            update_notification(notification, provider)
            try:
                send_sms_response(provider.get_name(), str(notification.id), notification.to)
            except:
                # when we retry, we only do anything if the notification is in created - it's currently in sending,
                # so set it back so that we actually attempt the callback again
                notification.sent_at = None
                notification.sent_by = None
                notification.status = NOTIFICATION_CREATED
                dao_update_notification(notification)
                raise
        else:
            try:
                sms_sender = dao_get_notification_sms_sender_mapping(notification.id)
                if not sms_sender:
                    sms_sender = service.get_default_sms_sender()

                provider.send_sms(
                    to=validate_and_format_phone_number(notification.to, international=notification.international),
                    content=str(template),
                    reference=str(notification.id),
                    sender=sms_sender
                )
            except Exception as e:
                dao_toggle_sms_provider(provider.name)
                raise e
            else:
                notification.billable_units = template.fragment_count
                update_notification(notification, provider, notification.international)

        create_initial_notification_statistic_tasks(notification)

        current_app.logger.info(
            "SMS {} sent to provider {} at {}".format(notification.id, provider.get_name(), notification.sent_at)
        )
        delta_milliseconds = (datetime.utcnow() - notification.created_at).total_seconds() * 1000
        statsd_client.timing("sms.total-time", delta_milliseconds)


def send_email_to_provider(notification):
    service = notification.service
    if not service.active:
        technical_failure(notification=notification)
        return
    if notification.status == 'created':
        provider = provider_to_use(EMAIL_TYPE, notification.id)
        current_app.logger.info(
            "Starting sending EMAIL {} to provider at {}".format(notification.id, datetime.utcnow())
        )
        template_dict = dao_get_template_by_id(notification.template_id, notification.template_version).__dict__

        html_email = HTMLEmailTemplate(
            template_dict,
            values=notification.personalisation,
            **get_html_email_options(service)
        )

        plain_text_email = PlainTextEmailTemplate(
            template_dict,
            values=notification.personalisation
        )

        if service.research_mode or notification.key_type == KEY_TYPE_TEST:
            reference = str(create_uuid())
            notification.billable_units = 0
            notification.reference = reference
            update_notification(notification, provider)
            send_email_response(provider.get_name(), reference, notification.to)
        else:
            from_address = '"{}" <{}@{}>'.format(service.name, service.email_from,
                                                 current_app.config['NOTIFY_EMAIL_DOMAIN'])

            email_reply_to = dao_get_notification_email_reply_for_notification(notification.id)

            if not email_reply_to:
                email_reply_to = service.get_default_reply_to_email_address()

            reference = provider.send_email(
                from_address,
                validate_and_format_email_address(notification.to),
                plain_text_email.subject,
                body=str(plain_text_email),
                html_body=str(html_email),
                reply_to_address=validate_and_format_email_address(email_reply_to) if email_reply_to else None,
            )
            notification.reference = reference
            update_notification(notification, provider)

        create_initial_notification_statistic_tasks(notification)

        current_app.logger.info(
            "Email {} sent to provider at {}".format(notification.id, notification.sent_at)
        )
        delta_milliseconds = (datetime.utcnow() - notification.created_at).total_seconds() * 1000
        statsd_client.timing("email.total-time", delta_milliseconds)


def update_notification(notification, provider, international=False):
    notification.sent_at = datetime.utcnow()
    notification.sent_by = provider.get_name()
    if international:
        notification.status = NOTIFICATION_SENT
    else:
        notification.status = NOTIFICATION_SENDING
    dao_update_notification(notification)


def provider_to_use(notification_type, notification_id, international=False):
    active_providers_in_order = [
        p for p in get_provider_details_by_notification_type(notification_type, international) if p.active
    ]

    if not active_providers_in_order:
        current_app.logger.error(
            "{} {} failed as no active providers".format(notification_type, notification_id)
        )
        raise Exception("No active {} providers".format(notification_type))

    return clients.get_client_by_name_and_type(active_providers_in_order[0].identifier, notification_type)


def get_logo_url(base_url, logo_file):
    base_url = parse.urlparse(base_url)
    netloc = base_url.netloc

    if base_url.netloc.startswith('localhost'):
        netloc = 'notify.tools'
    elif base_url.netloc.startswith('www'):
        # strip "www."
        netloc = base_url.netloc[4:]

    logo_url = parse.ParseResult(
        scheme=base_url.scheme,
        netloc='static-logos.' + netloc,
        path=logo_file,
        params=base_url.params,
        query=base_url.query,
        fragment=base_url.fragment
    )
    return parse.urlunparse(logo_url)


def get_html_email_options(service):
    govuk_banner = service.branding not in (BRANDING_ORG, BRANDING_ORG_BANNER)
    brand_banner = service.branding == BRANDING_ORG_BANNER
    if service.organisation and service.branding != BRANDING_GOVUK:

        logo_url = get_logo_url(
            current_app.config['ADMIN_BASE_URL'],
            service.organisation.logo
        ) if service.organisation.logo else None

        branding = {
            'brand_colour': service.organisation.colour,
            'brand_logo': logo_url,
            'brand_name': service.organisation.name,
        }
    else:
        branding = {}

    return dict(govuk_banner=govuk_banner, brand_banner=brand_banner, **branding)


def technical_failure(notification):
    notification.status = NOTIFICATION_TECHNICAL_FAILURE
    dao_update_notification(notification)
    current_app.logger.warn(
        "Send {} for notification id {} to provider is not allowed: service {} is inactive".format(
            notification.notification_type,
            notification.id,
            notification.service_id))
