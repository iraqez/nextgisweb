# -*- coding: utf-8 -*-
from sqlalchemy.orm.exc import NoResultFound
from bunch import Bunch

from pyramid.httpexceptions import HTTPFound, HTTPForbidden
from pyramid.security import remember, forget

from ..object_widget import ObjectWidget
from ..views import ModelController, permalinker
from .. import dynmenu as dm


def setup_pyramid(comp, config):
    Principal = comp.Principal
    User = comp.User
    Group = comp.Group

    def check_permission(request):
        """ Чтобы избежать перекрестной зависимости двух компонентов -
        auth и security, права доступа к редактированию пользователей
        ограничиваются по критерию членства в группе administrators """

        administrators = Group.filter_by(keyname='administrators').one()
        if request.user not in administrators.members:
            raise HTTPForbidden()

    def login(request):
        next = request.params.get('next', request.application_url)

        if request.method == 'POST':
            try:
                user = User.filter_by(keyname=request.POST['login']).one()

                if user.password == request.POST['password']:
                    headers = remember(request, user.id)
                    return HTTPFound(location=next, headers=headers)
                else:
                    raise NoResultFound()

            except NoResultFound:
                pass

        return dict()

    config.add_route('auth.login', '/login') \
        .add_view(login, renderer='auth/login.mako')

    def logout(request):
        headers = forget(request)
        return HTTPFound(location=request.application_url, headers=headers)

    config.add_route('auth.logout', '/logout').add_view(logout)

    def forbidden(request):
        # Если это гость, то аутентификация может ему помочь
        if request.user.keyname == 'guest':
            return HTTPFound(location=request.route_url('auth.login'))

        # Уже аутентифицированным пользователям показываем сообщение об ошибке
        return dict(subtitle=u"Отказано в доступе")

    config.add_view(
        forbidden,
        context=HTTPForbidden,
        renderer='auth/forbidden.mako'
    )

    def principal_dump(request):
        query = Principal.query().with_polymorphic('*')
        result = []

        for p in query:
            result.append(dict(
                id=p.id,
                cls=p.cls,
                keyname=p.keyname,
                display_name=p.display_name
            ))

        return result

    config.add_route('auth.principal_dump', '/auth/principal/dump') \
        .add_view(principal_dump, renderer='json')

    class AuthUserWidget(ObjectWidget):

        def is_applicable(self):
            return self.operation in ('create', 'edit')

        def populate_obj(self):
            super(AuthUserWidget, self).populate_obj()

            self.obj.display_name = self.data['display_name']
            self.obj.keyname = self.data['keyname']

            if self.data.get('password', None) is not None:
                self.obj.password = self.data['password']

            self.obj.member_of = map(
                lambda id: Group.filter_by(id=id).one(),
                self.data['member_of']
            )

        def validate(self):
            result = super(AuthUserWidget, self).validate()
            self.error = []

            return result

        def widget_params(self):
            result = super(AuthUserWidget, self).widget_params()

            if self.obj:
                result['value'] = dict(
                    display_name=self.obj.display_name,
                    keyname=self.obj.keyname,
                    member_of=[m.id for m in self.obj.member_of],
                )
                result['groups'] = [
                    dict(
                        value=g.id,
                        label=g.display_name,
                        selected=g in self.obj.member_of
                    )
                    for g in Group.query()
                ]

            else:
                # Список всех групп для поля выбора
                result['groups'] = [
                    dict(value=g.id, label=g.display_name)
                    for g in Group.query()
                ]

            return result

        def widget_module(self):
            return 'ngw/auth/UserWidget'

    class UserModelController(ModelController):

        def create_context(self, request):
            check_permission(request)
            return dict(
                template=dict(subtitle=u"Новый пользователь")
            )

        def edit_context(self, request):
            check_permission(request)
            obj = User.filter_by(**request.matchdict) \
                .filter_by(system=False).one()

            return dict(
                obj=obj,
                template=dict(obj=obj)
            )

        def create_object(self, context):
            return User()

        def query_object(self, context):
            return context['obj']

        def widget_class(self, context, operation):
            return AuthUserWidget

        def template_context(self, context):
            return context['template']

    UserModelController('auth.user', '/auth/user') \
        .includeme(config)

    permalinker(User, "auth.user.edit")

    def user_browse(request):
        check_permission(request)
        return dict(
            obj_list=User.filter_by(system=False).order_by(User.display_name),
            dynmenu=User.__dynmenu__,
            dynmenu_kwargs=Bunch(request=request),
        )

    config.add_route('auth.user.browse', '/auth/user/') \
        .add_view(user_browse, renderer='auth/user_browse.mako')

    class UserMenu(dm.DynItem):

        def build(self, kwargs):
            if 'obj' in kwargs:
                yield dm.Link(
                    'operation/edit',
                    u"Редактировать",
                    lambda kwargs: kwargs.request.route_url(
                        'auth.user.edit',
                        id=kwargs.obj.id
                    )
                )

    User.__dynmenu__ = dm.DynMenu(
        dm.Label('operation', u"Операции"),
        dm.Link(
            'operation/create',
            u"Создать",
            lambda kwargs: kwargs.request.route_url('auth.user.create')
        ),
        UserMenu()
    )
