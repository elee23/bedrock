# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import re

from django.conf import settings
from django.core.context_processors import csrf
from django.db.utils import DatabaseError
from django.http import HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import last_modified, require_safe
from django.views.generic import FormView, TemplateView
from django.shortcuts import redirect, render as django_render

import basket
import requests
from lib import l10n_utils
from commonware.decorators import xframe_allow
from funfactory.urlresolvers import reverse
from lib.l10n_utils.dotlang import _, lang_file_is_active

from bedrock.mozorg import email_contribute
from bedrock.mozorg.credits import CreditsFile
from bedrock.mozorg.decorators import cache_control_expires
from bedrock.mozorg.forms import (ContributeForm,
                                  ContributeStudentAmbassadorForm,
                                  WebToLeadForm, ContributeSignupForm)
from bedrock.mozorg.forums import ForumsFile
from bedrock.mozorg.models import TwitterCache
from bedrock.mozorg.util import hide_contrib_form
from bedrock.mozorg.util import HttpResponseJSON
from bedrock.newsletter.forms import NewsletterFooterForm


credits_file = CreditsFile('credits')
forums_file = ForumsFile('forums')


def csrf_failure(request, reason=''):
    template_vars = {'reason': reason}
    return l10n_utils.render(request, 'mozorg/csrf-failure.html', template_vars,
                             status=403)


@xframe_allow
def hacks_newsletter(request):
    return l10n_utils.render(request,
                             'mozorg/newsletter/hacks.mozilla.org.html')


class ContributeSignup(l10n_utils.LangFilesMixin, FormView):
    template_name = 'mozorg/contribute/signup.html'
    form_class = ContributeSignupForm
    category = None

    def get_form_kwargs(self):
        kwargs = super(ContributeSignup, self).get_form_kwargs()
        kwargs['locale'] = l10n_utils.get_locale(self.request)
        return kwargs

    def get_success_url(self):
        category = self.category or 'dontknow'
        return reverse('mozorg.contribute.thankyou') + '?c=' + category

    def get_basket_data(self, form):
        data = form.cleaned_data
        self.category = data['category']
        basket_data = {
            'email': data['email'],
            'name': data['name'],
            'country': data['country'],
            'message': data['message'],
            'subscribe': data['newsletter'],
            'lang': form.locale,
        }
        if 'area_' + self.category in data:
            interest_id = data['area_' + self.category]
        else:
            interest_id = self.category
        basket_data['interest_id'] = interest_id
        basket_data['source_url'] = self.request.build_absolute_uri()
        return basket_data

    def form_valid(self, form):
        try:
            basket.request('post', 'get-involved', self.get_basket_data(form))
        except basket.BasketException:
            msg = form.error_class(
                [_('We apologize, but an error occurred in our system. '
                   'Please try again later.')])
            form.errors['__all__'] = msg
            return self.form_invalid(form)

        return super(ContributeSignup, self).form_valid(form)


class ContributeSignupThankyou(l10n_utils.LangFilesMixin, TemplateView):
    template_name = 'mozorg/contribute/thankyou.html'
    category_re = re.compile('^\w{5,20}$')

    def get_context_data(self, **kwargs):
        cxt = super(ContributeSignupThankyou, self).get_context_data(**kwargs)
        category = self.request.GET.get('c', '')
        match = self.category_re.match(category)
        if match:
            cxt['category'] = category
        return cxt


@csrf_exempt
def contribute(request, template, return_to_form):
    newsletter_form = NewsletterFooterForm('about-mozilla', l10n_utils.get_locale(request))

    contribute_success = False

    form = ContributeForm(request.POST or None, auto_id=u'id_contribute-%s')
    if form.is_valid():
        data = form.cleaned_data.copy()

        honeypot = data.pop('office_fax')

        if not honeypot:
            contribute_success = email_contribute.handle_form(request, form)
            if contribute_success:
                # If form was submitted successfully, return a new, empty
                # one.
                form = ContributeForm()
        else:
            # send back a clean form if honeypot was filled in
            form = ContributeForm()

    return l10n_utils.render(request,
                             template,
                             {'form': form,
                              'newsletter_form': newsletter_form,
                              'contribute_success': contribute_success,
                              'return_to_form': return_to_form,
                              'hide_form': hide_contrib_form(request.locale)})


@xframe_allow
@csrf_exempt
def contribute_embed(request, template, return_to_form):
    """The same as contribute but allows frame embedding."""
    return contribute(request, template, return_to_form)


def process_partnership_form(request, template, success_url_name, template_vars=None, form_kwargs=None):
    template_vars = template_vars or {}
    form_kwargs = form_kwargs or {}

    if request.method == 'POST':
        form = WebToLeadForm(data=request.POST, **form_kwargs)

        msg = 'Form invalid'
        stat = 400
        success = False

        if form.is_valid():
            data = form.cleaned_data.copy()

            honeypot = data.pop('office_fax')

            if honeypot:
                msg = 'Visitor invalid'
                stat = 400
            else:
                interest = data.pop('interest')
                data['00NU0000002pDJr'] = interest
                data['oid'] = '00DU0000000IrgO'
                data['lead_source'] = form_kwargs.get('lead_source', 'www.mozilla.org/about/partnerships/')
                # As we're doing the Salesforce POST in the background here,
                # `retURL` is never visited/seen by the user. I believe it
                # is required by Salesforce though, so it should hang around
                # as a placeholder (with a valid URL, just in case).
                data['retURL'] = ('http://www.mozilla.org/en-US/about/'
                                  'partnerships?success=1')

                r = requests.post('https://www.salesforce.com/servlet/'
                                  'servlet.WebToLead?encoding=UTF-8', data)
                msg = requests.status_codes._codes.get(r.status_code, ['error'])[0]
                stat = r.status_code

                success = True

        if request.is_ajax():
            return HttpResponseJSON({'msg': msg, 'errors': form.errors}, status=stat)
        # non-AJAX POST
        else:
            # if form is not valid, render template to retain form data/error messages
            if not success:
                template_vars.update(csrf(request))
                template_vars['form'] = form
                template_vars['form_success'] = success

                return l10n_utils.render(request, template, template_vars)
            # if form is valid, redirect to avoid refresh double post possibility
            else:
                return HttpResponseRedirect("%s?success" % (reverse(success_url_name)))
    # no form POST - build form, add CSRF, & render template
    else:
        # without auto_id set, all id's get prefixed with 'id_'
        form = WebToLeadForm(auto_id='%s', **form_kwargs)

        template_vars.update(csrf(request))
        template_vars['form'] = form
        template_vars['form_success'] = True if ('success' in request.GET) else False

        return l10n_utils.render(request, template, template_vars)


@csrf_protect
def partnerships(request):
    return process_partnership_form(request, 'mozorg/partnerships.html', 'mozorg.partnerships')


def plugincheck(request, template='mozorg/plugincheck.html'):
    """
    Renders the plugncheck template.
    """
    return l10n_utils.render(request, template)


@xframe_allow
def contribute_studentambassadors_landing(request):
    try:
        tweets = TwitterCache.objects.get(account='mozstudents').tweets
    except (TwitterCache.DoesNotExist, DatabaseError):
        tweets = []
    return l10n_utils.render(request,
                             'mozorg/contribute/studentambassadors/landing.html',
                             {'tweets': tweets})


@csrf_protect
def contribute_studentambassadors_join(request):
    form = ContributeStudentAmbassadorForm(request.POST or None)
    if form.is_valid():
        try:
            form.save()
        except basket.BasketException:
            msg = form.error_class(
                [_('We apologize, but an error occurred in our system. '
                   'Please try again later.')])
            form.errors['__all__'] = msg
        else:
            return redirect('mozorg.contribute.studentambassadors.thanks')
    return l10n_utils.render(
        request,
        'mozorg/contribute/studentambassadors/join.html', {'form': form}
    )


def holiday_calendars(request, template='mozorg/projects/holiday-calendars.html'):
    """Generate the table of holiday calendars from JSON."""
    calendars = []
    json_file = settings.MEDIA_ROOT + '/caldata/calendars.json'
    with open(json_file) as calendar_data:
        calendars = json.load(calendar_data)

    letters = set()
    for calendar in calendars:
        letters.add(calendar['country'][:1])

    data = {
        'calendars': sorted(calendars, key=lambda k: k['country']),
        'letters': sorted(letters),
        'CALDATA_URL': settings.MEDIA_URL + 'caldata/'
    }

    return l10n_utils.render(request, template, data)


@cache_control_expires(2)
@last_modified(credits_file.last_modified_callback)
@require_safe
def credits_view(request):
    """Display the names of our contributors."""
    ctx = {'credits': credits_file}
    # not translated
    return django_render(request, 'mozorg/credits.html', ctx)


@cache_control_expires(2)
@last_modified(forums_file.last_modified_callback)
@require_safe
def forums_view(request):
    """Display our mailing lists and newsgroups."""
    ctx = {'forums': forums_file}
    return l10n_utils.render(request, 'mozorg/about/forums/forums.html', ctx)


class Robots(TemplateView):
    template_name = 'mozorg/robots.txt'

    def render_to_response(self, context, **response_kwargs):
        response_kwargs['content_type'] = 'text/plain'
        return super(Robots, self).render_to_response(
            context, **response_kwargs)

    def get_context_data(self, **kwargs):
        SITE_URL = getattr(settings, 'SITE_URL', '')
        return {'disallow_all': not SITE_URL.endswith('://www.mozilla.org')}


class HomeTestView(l10n_utils.LangFilesMixin, TemplateView):
    """Home page view that will use a different template for a QS."""
    template_name = 'mozorg/home.html'

    def post(self, request, *args, **kwargs):
        # required for newsletter form post that is handled in newsletter/helpers.py
        return self.get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super(HomeTestView, self).get_context_data(**kwargs)
        ctx['has_contribute'] = lang_file_is_active('mozorg/contribute')
        locale = l10n_utils.get_locale(self.request)
        locale = locale if locale in settings.MOBILIZER_LOCALE_LINK else 'en-US'
        ctx['mobilizer_link'] = settings.MOBILIZER_LOCALE_LINK[locale]

        return ctx
