"""
Generic models which provide extra functionality over base Django model types.
"""

from __future__ import unicode_literals

import re
import os
import logging

from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from mptt.models import MPTTModel, TreeForeignKey
from mptt.exceptions import InvalidMove

from .validators import validate_tree_name


logger = logging.getLogger('inventree')


def rename_attachment(instance, filename):
    """
    Function for renaming an attachment file.
    The subdirectory for the uploaded file is determined by the implementing class.

        Args:
        instance: Instance of a PartAttachment object
        filename: name of uploaded file

    Returns:
        path to store file, format: '<subdir>/<id>/filename'
    """

    # Construct a path to store a file attachment for a given model type
    return os.path.join(instance.getSubdir(), filename)


class ReferenceIndexingMixin(models.Model):
    """
    A mixin for keeping track of numerical copies of the "reference" field.

    Here, we attempt to convert a "reference" field value (char) to an integer,
    for performing fast natural sorting.

    This requires extra database space (due to the extra table column),
    but is required as not all supported database backends provide equivalent casting.

    This mixin adds a field named 'reference_int'.

    - If the 'reference' field can be cast to an integer, it is stored here
    - If the 'reference' field *starts* with an integer, it is stored here
    - Otherwise, we store zero
    """

    class Meta:
        abstract = True

    def rebuild_reference_field(self):

        reference = getattr(self, 'reference', '')

        # Default value if we cannot convert to an integer
        ref_int = 0

        # Look at the start of the string - can it be "integerized"?
        result = re.match(r"^(\d+)", reference)

        if result and len(result.groups()) == 1:
            ref = result.groups()[0]
            try:
                ref_int = int(ref)
            except:
                ref_int = 0

        self.reference_int = ref_int

    reference_int = models.IntegerField(default=0)


class InvenTreeAttachment(models.Model):
    """ Provides an abstracted class for managing file attachments.

    Attributes:
        attachment: File
        comment: String descriptor for the attachment
        user: User associated with file upload
        upload_date: Date the file was uploaded
    """
    def getSubdir(self):
        """
        Return the subdirectory under which attachments should be stored.
        Note: Re-implement this for each subclass of InvenTreeAttachment
        """

        return "attachments"

    def __str__(self):
        return os.path.basename(self.attachment.name)

    attachment = models.FileField(upload_to=rename_attachment, verbose_name=_('Attachment'),
                                  help_text=_('Select file to attach'))

    comment = models.CharField(blank=True, max_length=100, verbose_name=_('Comment'), help_text=_('File comment'))

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True, null=True,
        verbose_name=_('User'),
        help_text=_('User'),
    )

    upload_date = models.DateField(auto_now_add=True, null=True, blank=True, verbose_name=_('upload date'))

    @property
    def basename(self):
        return os.path.basename(self.attachment.name)

    @basename.setter
    def basename(self, fn):
        """
        Function to rename the attachment file.

        - Filename cannot be empty
        - Filename cannot contain illegal characters
        - Filename must specify an extension
        - Filename cannot match an existing file
        """

        fn = fn.strip()

        if len(fn) == 0:
            raise ValidationError(_('Filename must not be empty'))

        attachment_dir = os.path.join(
            settings.MEDIA_ROOT,
            self.getSubdir()
        )

        old_file = os.path.join(
            settings.MEDIA_ROOT,
            self.attachment.name
        )

        new_file = os.path.join(
            settings.MEDIA_ROOT,
            self.getSubdir(),
            fn
        )

        new_file = os.path.abspath(new_file)

        # Check that there are no directory tricks going on...
        if not os.path.dirname(new_file) == attachment_dir:
            logger.error(f"Attempted to rename attachment outside valid directory: '{new_file}'")
            raise ValidationError(_("Invalid attachment directory"))

        # Ignore further checks if the filename is not actually being renamed
        if new_file == old_file:
            return

        forbidden = ["'", '"', "#", "@", "!", "&", "^", "<", ">", ":", ";", "/", "\\", "|", "?", "*", "%", "~", "`"]

        for c in forbidden:
            if c in fn:
                raise ValidationError(_(f"Filename contains illegal character '{c}'"))

        if len(fn.split('.')) < 2:
            raise ValidationError(_("Filename missing extension"))

        if not os.path.exists(old_file):
            logger.error(f"Trying to rename attachment '{old_file}' which does not exist")
            return

        if os.path.exists(new_file):
            raise ValidationError(_("Attachment with this filename already exists"))

        try:
            os.rename(old_file, new_file)
            self.attachment.name = os.path.join(self.getSubdir(), fn)
            self.save()
        except:
            raise ValidationError(_("Error renaming file"))

    class Meta:
        abstract = True


class InvenTreeTree(MPTTModel):
    """ Provides an abstracted self-referencing tree model for data categories.

    - Each Category has one parent Category, which can be blank (for a top-level Category).
    - Each Category can have zero-or-more child Categor(y/ies)

    Attributes:
        name: brief name
        description: longer form description
        parent: The item immediately above this one. An item with a null parent is a top-level item
    """

    def api_instance_filters(self):
        """
        Instance filters for InvenTreeTree models
        """

        return {
            'parent': {
                'exclude_tree': self.pk,
            }
        }

    def save(self, *args, **kwargs):

        try:
            super().save(*args, **kwargs)
        except InvalidMove:
            raise ValidationError({
                'parent': _("Invalid choice"),
            })

    class Meta:
        abstract = True

        # Names must be unique at any given level in the tree
        unique_together = ('name', 'parent')

    class MPTTMeta:
        order_insertion_by = ['name']

    name = models.CharField(
        blank=False,
        max_length=100,
        validators=[validate_tree_name],
        verbose_name=_("Name"),
        help_text=_("Name"),
    )

    description = models.CharField(
        blank=True,
        max_length=250,
        verbose_name=_("Description"),
        help_text=_("Description (optional)")
    )

    # When a category is deleted, graft the children onto its parent
    parent = TreeForeignKey('self',
                            on_delete=models.DO_NOTHING,
                            blank=True,
                            null=True,
                            verbose_name=_("parent"),
                            related_name='children')

    @property
    def item_count(self):
        """ Return the number of items which exist *under* this node in the tree.

        Here an 'item' is considered to be the 'leaf' at the end of each branch,
        and the exact nature here will depend on the class implementation.

        The default implementation returns zero
        """
        return 0

    def getUniqueParents(self):
        """ Return a flat set of all parent items that exist above this node.
        If any parents are repeated (which would be very bad!), the process is halted
        """

        return self.get_ancestors()

    def getUniqueChildren(self, include_self=True):
        """ Return a flat set of all child items that exist under this node.
        If any child items are repeated, the repetitions are omitted.
        """

        return self.get_descendants(include_self=include_self)

    @property
    def has_children(self):
        """ True if there are any children under this item """
        return self.getUniqueChildren(include_self=False).count() > 0

    def getAcceptableParents(self):
        """ Returns a list of acceptable parent items within this model
        Acceptable parents are ones which are not underneath this item.
        Setting the parent of an item to its own child results in recursion.
        """

        contents = ContentType.objects.get_for_model(type(self))

        available = contents.get_all_objects_for_this_type()

        # List of child IDs
        childs = self.getUniqueChildren()

        acceptable = [None]

        for a in available:
            if a.id not in childs:
                acceptable.append(a)

        return acceptable

    @property
    def parentpath(self):
        """ Get the parent path of this category

        Returns:
            List of category names from the top level to the parent of this category
        """

        return [a for a in self.get_ancestors()]

    @property
    def path(self):
        """ Get the complete part of this category.

        e.g. ["Top", "Second", "Third", "This"]

        Returns:
            List of category names from the top level to this category
        """
        return self.parentpath + [self]

    @property
    def pathstring(self):
        """ Get a string representation for the path of this item.

        e.g. "Top/Second/Third/This"
        """
        return '/'.join([item.name for item in self.path])

    def __str__(self):
        """ String representation of a category is the full path to that category """

        return "{path} - {desc}".format(path=self.pathstring, desc=self.description)


@receiver(pre_delete, sender=InvenTreeTree, dispatch_uid='tree_pre_delete_log')
def before_delete_tree_item(sender, instance, using, **kwargs):
    """ Receives pre_delete signal from InvenTreeTree object.

    Before an item is deleted, update each child object to point to the parent of the object being deleted.
    """

    # Update each tree item below this one
    for child in instance.children.all():
        child.parent = instance.parent
        child.save()
