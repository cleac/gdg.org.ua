import logging
import re

from copy import deepcopy
from datetime import date
from uuid import uuid4

import cherrypy

from cherrypy import HTTPError
from cherrypy.lib import file_generator

from blueberrypy.util import from_collection, to_collection

from requests.exceptions import HTTPError as RequestsHTTPError

from . import api
from .errors import InvalidFormDataError
from .model import User, Event, EventParticipant, Invite

from .lib.utils.gdrive import gdrive_upload
from .lib.utils.mail import gmail_send_html
from .lib.utils.table_exporter import gen_participants_xlsx
from .lib.utils.signals import pub
from .lib.utils.vcard import make_vcard, aes_encrypt
from .lib.forms import (
    RegistrationForm, get_additional_fields_form_cls,
    InputDict,
)


logger = logging.getLogger(__name__)


class APIBase:
    _cp_config = {'tools.json_in.on': True}

    def create(self, **kwargs):
        raise NotImplementedError()

    def show(self, **kwargs):
        raise NotImplementedError()

    def list_all(self, **kwargs):
        raise NotImplementedError()

    def update(self, **kwargs):
        raise NotImplementedError()

    def delete(self, **kwargs):
        raise NotImplementedError()


class Admin(APIBase):
    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def info(self):
        req = cherrypy.request
        user = {'admin': True}
        user.update(req.admin_user)
        user.update(req.google_user)
        user.update(req.google_oauth_token)
        res = {'user': user}
        if user.get('filter_place'):
            res['place'] = to_collection(
                api.get_place_by_id(
                    cherrypy.request.orm_session,
                    req.admin_user['filter_place']))
        return res

    @cherrypy.tools.json_out()
    def sign_in(self):
        # Doc:
        # https://developers.google.com/identity/sign-in/web/backend-auth
        # #using-a-google-api-client-library

        req = cherrypy.request

        try:
            pub('oauth-code-token', req.json['access_code'])

            with pub('google-api') as google_api:
                # TODO: do whatever we need with google_api
                cherrypy.session['google_user'] = google_api.get(
                    'https://www.googleapis.com/oauth2/v1/userinfo').json()

                try:
                    cherrypy.session['admin_user'] = to_collection(
                        api.find_admin_by_email(
                            req.orm_session,
                            cherrypy.session['google_user']['email']))
                except:
                    # It seems he's not an admin. Forgive this
                    pass

                user_info = google_api.get(
                    'https://www.googleapis.com/plus/v1/people/{}'.format(
                        cherrypy.session['google_user']['id'])).json()
        except KeyError as ke:
            raise HTTPError(400, 'Missing input parameter') from ke
        except RequestsHTTPError as httperr:
            raise HTTPError(400, 'Invalid user data') from httperr
        except Exception as exc:
            raise HTTPError(500, 'Some unexpected error happened') from exc
        else:
            return user_info


class Participants(APIBase):

    @cherrypy.tools.json_out()
    def create(self, **kwargs):
        req = cherrypy.request
        orm_session = req.orm_session

        try:
            event_id = int(req.json['event'])
        except (ValueError, TypeError, KeyError):
            raise HTTPError(400, 'Invalid `event` param')

        event = api.find_event_by_id(orm_session, event_id)
        if not event:
            raise HTTPError(404, 'Event not found')

        # Get request data
        u = req.json.get('user', {})
        fields = req.json.get('fields')

        # Validate form data
        regform = RegistrationForm(hidden=None, formdata=InputDict(u))
        fieldsform_cls = get_additional_fields_form_cls(event.fields)
        fieldsform = fieldsform_cls(InputDict(fields))
        if not all([regform.validate(), fieldsform.validate()]):
            errors = deepcopy(regform.errors)
            errors.update(fieldsform.errors)
            raise InvalidFormDataError(errors)

        # Registration BL
        user = User(**u)

        invitation = None
        if req.json.get('invite_code'):
            invitation = api.find_invitation_by_code(
                orm_session, req.json['invite_code']
            )

            # check if the invitation is valid
            if (
                invitation is None or invitation.used or
                (invitation.event and invitation.event.id != event.id) or
                (invitation.email is not None and invitation.email != user.email)
            ):
                raise HTTPError(403, 'Invalid invite code.')

        eu = api.find_user_by_email(orm_session, user.email)
        if eu:
            user.id = eu.id
            orm_session.merge(user)
        else:
            orm_session.add(user)
        orm_session.flush()

        eep = api.get_event_registration(orm_session, user.id, event.id)
        ep = EventParticipant(
            id=eep.id if eep else None,
            event_id=event.id,
            googler_id=user.id,
            register_date=date.today(),
            fields=fields,
        )

        if eep:
            orm_session.merge(ep)
        else:
            orm_session.add(ep)
        if invitation is not None:
            invitation.email = user.email
            invitation.used = True
            orm_session.merge(invitation)
        orm_session.commit()

        return to_collection(user, sort_keys=True)

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def show(self, id, **kwargs):
        id = int(id)
        user = api.find_user_by_id(cherrypy.request.orm_session, id)
        if user:
            events = api.find_events_by_user(cherrypy.request.orm_session,
                                             user)
            logger.debug(events)
            u = to_collection(user, excludes=('password', 'salt'),
                              sort_keys=True)
            u.update({'events': [
                to_collection(e, sort_keys=True) for e in events]})
            logger.debug(u)
            return u
        raise HTTPError(404)

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def list_all(self, **kwargs):
        logger.debug('listing users')
        users = api.get_all_users(cherrypy.request.orm_session)
        if users:
            return [to_collection(
                u, excludes=('password', 'salt'), sort_keys=True)
                for u in users]
        raise HTTPError(404)

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def update(self, id, **kwargs):
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session
        user = api.find_user_by_id(orm_session, id)
        if user:
            user = from_collection(req.json, user)
            orm_session.merge(user)
            orm_session.commit()
            return to_collection(user, excludes=('password', 'salt'),
                                 sort_keys=True)
        raise HTTPError(404)

    @cherrypy.tools.authorize()
    def delete(self, id, **kwargs):
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session
        if not api.delete_user_by_id(orm_session, id):
            raise HTTPError(404)
        else:
            orm_session.commit()


class Events(APIBase):

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def create(self, **kwargs):
        req = cherrypy.request
        orm_session = req.orm_session
        event = from_collection(req.json, Event())
        orm_session.add(event)
        orm_session.commit()
        return to_collection(event, sort_keys=True)

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def show(self, id, **kwargs):
        id = int(id)
        event = api.find_event_by_id(cherrypy.request.orm_session, id)
        if event:
            registrations = api.get_event_registrations(
                cherrypy.request.orm_session, event.id)
            logger.debug(registrations)
            e = to_collection(event, sort_keys=True)
            e.update({'invites': [to_collection(i, sort_keys=True)
                     for i in event.invites]})
            e.update({'registrations': [to_collection(r, sort_keys=True)
                     for r in registrations]})
            for r in e['registrations']:
                r.update({'cardUrl': aes_encrypt(str(r['id']))})
                r.update({'participant': to_collection(
                    api.find_user_by_id(cherrypy.request.orm_session,
                                        r['googler_id']),
                    excludes=('password', 'salt'))})
            logger.debug(e)
            return e
        raise HTTPError(404)

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def list_all(self, **kwargs):
        events = api.get_all_events(cherrypy.request.orm_session)
        return [to_collection(e, sort_keys=True)
                for e in events] if events else []

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def update(self, id, **kwargs):
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session
        event = api.find_event_by_id(orm_session, id)
        logger.debug(event)
        if event:
            # Caution! crunches ahead
            event = from_collection(req.json, event,
                                    excludes=['fields'])  # skip jsonencoded
            # since 'hidden' is not implemented in the model, skip it for now
            event.fields = req.json['fields']  # and set them manually
            orm_session.merge(event)
            orm_session.commit()
            return to_collection(event, sort_keys=True)
        raise HTTPError(404)

    @cherrypy.tools.authorize()
    def delete(self, id, **kwargs):
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session
        if not api.delete_event_by_id(orm_session, id):
            raise HTTPError(404)
        else:
            orm_session.commit()

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def approve_participants(self, id, **kwargs):
        '''POST /api/events/:id/approve'''
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session
        try:
            regs = req.json.get('registrations')
            from_email = (req.json.get('fromEmail') or
                          'GDG Registration Robot <kyiv@gdg.org.ua>')
            send_email = req.json.get('sendEmail')

            subject = '✔ Registration confirmation to {event_title}'
            to_email = '{full_name} <{email}>'
            email_template = 'email/card.html'

            event = api.find_event_by_id(orm_session, id)

            for user_reg in api.get_event_registrations_by_ids(
                    orm_session, [int(_) for _ in regs]):

                u = user_reg.user
                user_reg.accepted = True

                orm_session.merge(user_reg)
                orm_session.commit()

                if send_email:  # Do send email here
                    gmail_send_html(
                        template=email_template,
                        payload={'event': event, 'user': u,
                                 'registration': user_reg,
                                 'qrdata': make_vcard(user_reg)},
                        sbj=subject.format(event_title=event.title),
                        to_email=to_email.format(full_name=u.full_name,
                                                 email=u.email),
                        from_email=from_email)
        except KeyError:
            logger.exception('Could not send confirmation request')
            raise HTTPError(400, {'ok': False})
        else:
            return {'ok': True}

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def send_confirm_participants(self, id, **kwargs):
        '''POST /api/events/:id/send-confirm'''
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session
        try:
            regs = req.json.get('registrations')
            from_email = (req.json.get('fromEmail') or
                          'GDG Registration Robot <kyiv@gdg.org.ua>')

            subject = 'Please confirm your visit to {event_title}'
            to_email = '{full_name} <{email}>'
            email_template = 'email/confirmation.html'

            event = api.find_event_by_id(orm_session, id)

            for user_reg in api.get_event_registrations_by_ids(
                    orm_session, [int(_) for _ in regs]):
                logger.debug(user_reg)
                u = user_reg.user

                secure_id = aes_encrypt(str(user_reg.id))

                confirm_data = {
                    'url': '/confirm/{id}'.format(id=secure_id)
                }
                logger.debug(confirm_data)

                # Do send email here
                gmail_send_html(
                    template=email_template,
                    payload={'event': event, 'user': u,
                             'registration': user_reg,
                             'confirm': confirm_data},
                    sbj=subject.format(event_title=event.title),
                    to_email=to_email.format(full_name=u.full_name,
                                             email=u.email),
                    from_email=from_email)
        except KeyError:
            logger.exception('Could not send confirmation request')
            raise HTTPError(400, "{'ok': False}")
        else:
            return {'ok': True}

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def resend_approve_participants(self, id, **kwargs):
        '''POST /api/events/:id/resend'''
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session
        try:
            user_id = int(req.json.get('id'))
            from_email = (req.json.get('fromEmail') or
                          'GDG Registration Robot <kyiv@gdg.org.ua>')

            subject = '✔ Registration confirmation to {event_title}'
            to_email = '{full_name} <{email}>'
            email_template = 'email/card.html'

            user_reg = api.get_event_registration(orm_session, user_id, id)
            event = user_reg.event
            user = user_reg.user

            gmail_send_html(
                template=email_template,
                payload={'event': event, 'user': user,
                         'registration': user_reg},
                sbj=subject.format(event_title=event.title),
                to_email=to_email.format(full_name=user.full_name,
                                         email=user.email),
                from_email=from_email)
        except KeyError:
            raise HTTPError(400, {'ok': False})
        else:
            return {'ok': True}

    @cherrypy.tools.authorize()
    def export_participants(self, id):
        """Exports xlsx file with event participants

        Args:
            id (int): event id
        """
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session

        # Retrieve event object
        event = api.find_event_by_id(orm_session, id)
        if event is None:
            raise HTTPError(404)

        # Set appropriate headers
        filename = re.compile(r'[^\w-]').sub('', event.title.replace(' ', '_'))

        cherrypy.response.headers['Content-Type'] = (
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        cherrypy.response.headers['Content-Disposition'] = (
            'attachment; filename={}-{}-{}-participants.xlsx'.format(
                event.id, filename, event.date,
            )
        )

        # Retrieve participation data
        participations = api.find_participants_by_event(orm_session, event)

        return file_generator(gen_participants_xlsx(participations))

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def generate_report(self, id, mode=None):
        """Exports spreadsheet file with event participants to Google Drive

        Args:
            id (int): event id
            mode (str): tells us to filter out some entries,
                        valid values: all, approved, waiting
        """
        id = int(id)
        req = cherrypy.request
        orm_session = req.orm_session

        # Retrieve event object
        event = api.find_event_by_id(orm_session, id)
        if event is None:
            raise HTTPError(404)

        file_mime = ('application/vnd.openxmlformats-officedocument'
                     '.spreadsheetml.sheet')
        file_name = 'Participants of [#{}] {} on {}'.format(
            event.id, event.title, event.date)

        # Retrieve participation data
        participations = api.find_participants_by_event(orm_session, event)

        if mode == 'approved':  # return only accepted guys
            participations = filter(lambda _: _.EventParticipant.accepted,
                                    participations)
        elif mode == 'waiting':  # return ones waiting for approval
            participations = filter(lambda _: not _.EventParticipant.accepted,
                                    participations)

        # Upload to Google Drive
        gd_resp = gdrive_upload(
            file_name, file_mime,
            gen_participants_xlsx(participations).getvalue())

        return {'url': gd_resp['alternateLink']}

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def generate_invites(self, id):
        req = cherrypy.request
        orm_session = req.orm_session
        data = req.json

        try:
            number = data['number']
            assert number >= 0
        except (TypeError, KeyError, AssertionError) as e:
            # Type- or KeyError if data is None or has no 'number'
            # AssertionError if number of invites is negative
            logger.exception()
            raise HTTPError(400, 'Malformed request body') from e

        event = api.find_event_by_id(orm_session, id)
        if event is None:
            raise HTTPError(404)

        for _ in range(number):
            code = uuid4().hex
            invite = Invite(code=code, event=event)
            orm_session.add(invite)
        try:
            orm_session.commit()
        except Exception as e:
            # If here, then smth bad happened during
            # saving invites to db. We need to rollback.
            orm_session.rollback()
            logger.exception()
            raise HTTPError(500, 'Cannot save generated invites') from e
        return {'ok': True}

    @cherrypy.tools.json_out()
    @cherrypy.tools.authorize()
    def record_visit(self, id):
        '''POST /api/events/:id/check-in'''
        req = cherrypy.request
        orm_session = req.orm_session
        reg_id = int(id)
        reg_data = api.get_event_registration_by_id(orm_session, reg_id)
        if not reg_data:
            raise HTTPError(400,
                            'There is no registration record'
                            'for id={id}'.format(id=reg_id))
        reg_data.visited = True
        orm_session.merge(reg_data)
        orm_session.commit()
        return to_collection(reg_data, sort_keys=True)


class Places(APIBase):
    @cherrypy.tools.json_out()
    def list_all(self, **kwargs):
        places = api.get_all_gdg_places(cherrypy.request.orm_session)
        if places:
            return [to_collection(p, sort_keys=True) for p in places]
        raise HTTPError(404)

rest_api = cherrypy.dispatch.RoutesDispatcher()
rest_api.mapper.explicit = False
rest_api.connect('add_participant', '/participants', Participants,
                 action='create', conditions={'method': ['POST']})
rest_api.connect('list_participants', '/participants', Participants,
                 action='list_all', conditions={'method': ['GET']})
rest_api.connect('get_participant', '/participants/{id}', Participants,
                 action='show', conditions={'method': ['GET']})
rest_api.connect('edit_participant', '/participants/{id}', Participants,
                 action='update', conditions={'method': ['PUT']})
# rest_api.connect("remove_participant", "/participants/{id}", Participants,
#                  action="delete", conditions={"method": ["DELETE"]})

rest_api.connect('api_add_event', '/events', Events, action='create',
                 conditions={'method': ['POST']})
rest_api.connect('api_list_events', '/events', Events, action='list_all',
                 conditions={'method': ['GET']})
rest_api.connect('api_get_event', '/events/{id}', Events, action='show',
                 conditions={'method': ['GET']})
rest_api.connect('api_edit_event', '/events/{id}', Events, action='update',
                 conditions={'method': ['PUT']})
# rest_api.connect("remove_event", "/events/{id:\d+}", Events, action="delete",
#                  conditions={"method": ["DELETE"]})
# rest_api.connect("delete_event", "/events/{id:\d+}/delete", Events,
#                  action="delete",
#                  conditions={"method": ["POST"]})
rest_api.connect('generate_invites', '/events/{id:\d+}/invites', Events,
                 action='generate_invites',
                 conditions={'method': ['POST']})
rest_api.connect('generate_report', '/events/{id:\d+}/report', Events,
                 action='generate_report',
                 conditions={'method': ['POST']})

rest_api.connect('approve_event_participants',
                 r'/events/{id:\d+}/approve', Events,
                 action='approve_participants',
                 conditions={'method': ['POST']})
rest_api.connect('send_confirm_event_participants',
                 r'/events/{id:\d+}/send-confirm', Events,
                 action='send_confirm_participants',
                 conditions={'method': ['POST']})
rest_api.connect('resend_approve_event_participants',
                 r'/events/{id:\d+}/resend', Events,
                 action='resend_approve_participants',
                 conditions={'method': ['POST']})
rest_api.connect('export_event_participants',
                 r'/events/{id:\d+}/export_participants', Events,
                 action='export_participants',
                 conditions={'method': ['GET']})

rest_api.connect('list_places', '/places', Places, action='list_all',
                 conditions={'method': ['GET']})

rest_api.connect('api_info', '/info', Admin, action='info',
                 conditions={'method': ['GET']})
rest_api.connect('sign-in', '/sign-in', Admin, action='sign_in',
                 conditions={'method': ['POST']})
rest_api.connect('check-in',
                 r'/events/{id:\d+}/check-in', Events,
                 action='record_visit',
                 conditions={'method': ['POST']})
