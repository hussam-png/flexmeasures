from typing import Dict, List, Union, Optional
import random
import string

from flask import current_app
from flask_security import current_user, SQLAlchemySessionUserDatastore
from flask_security.recoverable import update_password
from email_validator import (
    validate_email,
    validate_email_deliverability,
    EmailNotValidError,
    EmailUndeliverableError,
)
from werkzeug.exceptions import NotFound

from flexmeasures.data.config import db
from flexmeasures.data.models.data_sources import DataSource
from flexmeasures.data.models.user import User, Role


class InvalidFlexMeasuresUser(Exception):
    pass


def get_user(id: str) -> User:
    """Get a user, raise if not found."""
    user: User = User.query.filter_by(id=int(id)).one_or_none()
    if user is None:
        raise NotFound
    return user


def get_users(role_name: Optional[str] = None, only_active: bool = True) -> List[User]:
    """Return a list of User objects.
    The role_name parameter allows to filter by role.
    Set only_active to False if you also want non-active users.
    """
    user_query = User.query
    if only_active:
        user_query = user_query.filter(User.active.is_(True))

    if role_name is not None:
        role = Role.query.filter(Role.name == role_name).one_or_none()
        if role:
            user_query = user_query.filter(User.flexmeasures_roles.contains(role))

    return user_query.all()


def find_user_by_email(user_email: str, keep_in_session: bool = True) -> User:
    user_datastore = SQLAlchemySessionUserDatastore(db.session, User, Role)
    user = user_datastore.find_user(email=user_email)
    if not keep_in_session:
        # we might need this object persistent across requests
        db.session.expunge(user)
    return user


def create_user(  # noqa: C901
    user_roles: Union[Dict[str, str], List[Dict[str, str]], str, List[str]] = None,
    check_deliverability: bool = True,
    **kwargs
) -> User:
    """Convenience wrapper to create a new User object and new Role objects (if user roles do not already exist),
    and new DataSource object that corresponds to the user."""

    # Check necessary input explicitly before anything happens
    if "email" not in kwargs:
        raise InvalidFlexMeasuresUser("No email address provided.")
    email = kwargs.pop("email").strip()
    try:
        email_info = validate_email(email, check_deliverability=False)
        # The mx check talks to the SMTP server. During testing, we skip it because it
        # takes a bit of time and without internet connection it fails.
        if check_deliverability and not current_app.testing:
            try:
                validate_email_deliverability(
                    email_info.domain, email_info["domain_i18n"]
                )
            except EmailUndeliverableError as eue:
                raise InvalidFlexMeasuresUser(
                    "The email address %s does not seem to be deliverable: %s"
                    % (email, str(eue))
                )
    except EmailNotValidError as enve:
        raise InvalidFlexMeasuresUser(
            "%s is not a valid email address: %s" % (email, str(enve))
        )
    if "username" not in kwargs:
        username = email.split("@")[0]
    else:
        username = kwargs.pop("username").strip()

    # Check integrity explicitly before anything happens
    existing_user_by_email = User.query.filter_by(email=email).one_or_none()
    if existing_user_by_email is not None:
        raise InvalidFlexMeasuresUser("User with email %s already exists." % email)
    existing_user_by_username = User.query.filter_by(username=username).one_or_none()
    if existing_user_by_username is not None:
        raise InvalidFlexMeasuresUser(
            "User with username %s already exists." % username
        )

    user_datastore = SQLAlchemySessionUserDatastore(db.session, User, Role)
    kwargs.update(email=email, username=username)
    user = user_datastore.create_user(**kwargs)

    if user.password is None:
        new_random_password = "".join(
            [random.choice(string.ascii_lowercase) for _ in range(12)]
        )
        update_password(user, new_random_password)

    # add roles to user (creating new roles if necessary)
    if user_roles:
        if not isinstance(user_roles, list):
            user_roles = [user_roles]  # type: ignore
        for user_role in user_roles:
            if isinstance(user_role, dict):
                role = user_datastore.find_role(user_role["name"])
            else:
                role = user_datastore.find_role(user_role)
            if role is None:
                if isinstance(user_role, dict):
                    role = user_datastore.create_role(**user_role)
                else:
                    role = user_datastore.create_role(name=user_role)
            user_datastore.add_role_to_user(user, role)

    # create data source
    db.session.add(DataSource(user=user))

    return user


def delete_user(user: User):
    """Delete the user (and also his assets and power measurements!). Deleting oneself is not allowed."""
    if hasattr(current_user, "id") and user.id == current_user.id:
        raise Exception("You cannot delete yourself.")
    else:
        user_datastore = SQLAlchemySessionUserDatastore(db.session, User, Role)
        user_datastore.delete_user(user)
        db.session.delete(user)
        current_app.logger.info("Deleted %s." % user)
