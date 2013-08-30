#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '2011, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

from datetime import datetime, timedelta
from collections import defaultdict

from calibre.constants import plugins
from calibre.utils.date import parse_date, UNDEFINED_DATE, utc_tz
from calibre.ebooks.metadata import author_to_author_sort

_c_speedup = plugins['speedup'][0].parse_date

def c_parse(val):
    try:
        year, month, day, hour, minutes, seconds, tzsecs = _c_speedup(val)
    except (AttributeError, TypeError):
        # If a value like 2001 is stored in the column, apsw will return it as
        # an int
        if isinstance(val, (int, float)):
            return datetime(int(val), 1, 3, tzinfo=utc_tz)
    except:
        pass
    else:
        try:
            ans = datetime(year, month, day, hour, minutes, seconds, tzinfo=utc_tz)
            if tzsecs is not 0:
                ans -= timedelta(seconds=tzsecs)
        except OverflowError:
            ans = UNDEFINED_DATE
        return ans
    try:
        return parse_date(val, as_utc=True, assume_utc=True)
    except ValueError:
        return UNDEFINED_DATE

ONE_ONE, MANY_ONE, MANY_MANY = xrange(3)

null = object()

class Table(object):

    def __init__(self, name, metadata, link_table=None):
        self.name, self.metadata = name, metadata
        self.sort_alpha = metadata.get('is_multiple', False) and metadata.get('display', {}).get('sort_alpha', False)

        # self.unserialize() maps values from the db to python objects
        self.unserialize = {
            'datetime': c_parse,
            'bool': bool
        }.get(metadata['datatype'], None)
        if name == 'authors':
            # Legacy
            self.unserialize = lambda x: x.replace('|', ',') if x else ''

        self.link_table = (link_table if link_table else
                'books_%s_link'%self.metadata['table'])

    def remove_books(self, book_ids, db):
        return set()

    def fix_link_table(self, db):
        pass

class VirtualTable(Table):

    '''
    A dummy table used for fields that only exist in memory like ondevice
    '''

    def __init__(self, name, table_type=ONE_ONE, datatype='text'):
        metadata = {'datatype':datatype, 'table':name}
        self.table_type = table_type
        Table.__init__(self, name, metadata)

class OneToOneTable(Table):

    '''
    Represents data that is unique per book (it may not actually be unique) but
    each item is assigned to a book in a one-to-one mapping. For example: uuid,
    timestamp, size, etc.
    '''

    table_type = ONE_ONE

    def read(self, db):
        idcol = 'id' if self.metadata['table'] == 'books' else 'book'
        query = db.conn.execute('SELECT {0}, {1} FROM {2}'.format(idcol,
            self.metadata['column'], self.metadata['table']))
        if self.unserialize is None:
            try:
                self.book_col_map = dict(query)
            except UnicodeDecodeError:
                # The db is damaged, try to work around it by ignoring
                # failures to decode utf-8
                query = db.conn.execute('SELECT {0}, cast({1} as blob) FROM {2}'.format(idcol,
                    self.metadata['column'], self.metadata['table']))
                self.book_col_map = {k:bytes(val).decode('utf-8', 'replace') for k, val in query}
        else:
            us = self.unserialize
            self.book_col_map = {book_id:us(val) for book_id, val in query}

    def remove_books(self, book_ids, db):
        clean = set()
        for book_id in book_ids:
            val = self.book_col_map.pop(book_id, null)
            if val is not null:
                clean.add(val)
        return clean

class PathTable(OneToOneTable):

    def set_path(self, book_id, path, db):
        self.book_col_map[book_id] = path
        db.conn.execute('UPDATE books SET path=? WHERE id=?',
                        (path, book_id))

class SizeTable(OneToOneTable):

    def read(self, db):
        query = db.conn.execute(
            'SELECT books.id, (SELECT MAX(uncompressed_size) FROM data '
            'WHERE data.book=books.id) FROM books')
        self.book_col_map = dict(query)

    def update_sizes(self, size_map):
        self.book_col_map.update(size_map)

class UUIDTable(OneToOneTable):

    def read(self, db):
        OneToOneTable.read(self, db)
        self.uuid_to_id_map = {v:k for k, v in self.book_col_map.iteritems()}

    def update_uuid_cache(self, book_id_val_map):
        for book_id, uuid in book_id_val_map.iteritems():
            self.uuid_to_id_map.pop(self.book_col_map.get(book_id, None), None)  # discard old uuid
            self.uuid_to_id_map[uuid] = book_id

    def remove_books(self, book_ids, db):
        clean = set()
        for book_id in book_ids:
            val = self.book_col_map.pop(book_id, null)
            if val is not null:
                self.uuid_to_id_map.pop(val, None)
                clean.add(val)
        return clean

    def lookup_by_uuid(self, uuid):
        return self.uuid_to_id_map.get(uuid, None)

class CompositeTable(OneToOneTable):

    def read(self, db):
        self.book_col_map = {}
        d = self.metadata['display']
        self.composite_template = ['composite_template']
        self.contains_html = d.get('contains_html', False)
        self.make_category = d.get('make_category', False)
        self.composite_sort = d.get('composite_sort', False)
        self.use_decorations = d.get('use_decorations', False)

    def remove_books(self, book_ids, db):
        return set()

class ManyToOneTable(Table):

    '''
    Represents data where one data item can map to many books, for example:
    series or publisher.

    Each book however has only one value for data of this type.
    '''

    table_type = MANY_ONE

    def read(self, db):
        self.id_map = {}
        self.col_book_map = defaultdict(set)
        self.book_col_map = {}
        self.read_id_maps(db)
        self.read_maps(db)

    def read_id_maps(self, db):
        query = db.conn.execute('SELECT id, {0} FROM {1}'.format(
            self.metadata['column'], self.metadata['table']))
        if self.unserialize is None:
            self.id_map = dict(query)
        else:
            us = self.unserialize
            self.id_map = {book_id:us(val) for book_id, val in query}

    def read_maps(self, db):
        cbm = self.col_book_map
        bcm = self.book_col_map
        for book, item_id in db.conn.execute(
                'SELECT book, {0} FROM {1}'.format(
                    self.metadata['link_column'], self.link_table)):
            cbm[item_id].add(book)
            bcm[book] = item_id

    def fix_link_table(self, db):
        linked_item_ids = {item_id for item_id in self.book_col_map.itervalues()}
        extra_item_ids = linked_item_ids - set(self.id_map)
        if extra_item_ids:
            for item_id in extra_item_ids:
                book_ids = self.col_book_map.pop(item_id, ())
                for book_id in book_ids:
                    self.book_col_map.pop(book_id, None)
            db.conn.executemany('DELETE FROM {0} WHERE {1}=?'.format(
                self.link_table, self.metadata['link_column']), tuple((x,) for x in extra_item_ids))

    def remove_books(self, book_ids, db):
        clean = set()
        for book_id in book_ids:
            item_id = self.book_col_map.pop(book_id, None)
            if item_id is not None:
                try:
                    self.col_book_map[item_id].discard(book_id)
                except KeyError:
                    if self.id_map.pop(item_id, null) is not null:
                        clean.add(item_id)
                else:
                    if not self.col_book_map[item_id]:
                        del self.col_book_map[item_id]
                        if self.id_map.pop(item_id, null) is not null:
                            clean.add(item_id)
        if clean:
            db.conn.executemany(
                'DELETE FROM {0} WHERE id=?'.format(self.metadata['table']),
                [(x,) for x in clean])
        return clean

    def remove_items(self, item_ids, db):
        affected_books = set()
        for item_id in item_ids:
            val = self.id_map.pop(item_id, null)
            if val is null:
                continue
            book_ids = self.col_book_map.pop(item_id, set())
            for book_id in book_ids:
                self.book_col_map.pop(book_id, None)
            affected_books.update(book_ids)
        item_ids = tuple((x,) for x in item_ids)
        db.conn.executemany('DELETE FROM {0} WHERE {1}=?'.format(self.link_table, self.metadata['link_column']), item_ids)
        db.conn.executemany('DELETE FROM {0} WHERE id=?'.format(self.metadata['table']), item_ids)
        return affected_books

    def rename_item(self, item_id, new_name, db):
        rmap = {icu_lower(v):k for k, v in self.id_map.iteritems()}
        existing_item = rmap.get(icu_lower(new_name), None)
        table, col, lcol = self.metadata['table'], self.metadata['column'], self.metadata['link_column']
        affected_books = self.col_book_map.get(item_id, set())
        new_id = item_id
        if existing_item is None or existing_item == item_id:
            # A simple rename will do the trick
            self.id_map[item_id] = new_name
            db.conn.execute('UPDATE {0} SET {1}=? WHERE id=?'.format(table, col), (new_name, item_id))
        else:
            # We have to replace
            new_id = existing_item
            self.id_map.pop(item_id, None)
            books = self.col_book_map.pop(item_id, set())
            for book_id in books:
                self.book_col_map[book_id] = existing_item
            self.col_book_map[existing_item].update(books)
            # For custom series this means that the series index can
            # potentially have duplicates/be incorrect, but there is no way to
            # handle that in this context.
            db.conn.execute('UPDATE {0} SET {1}=? WHERE {1}=?; DELETE FROM {2} WHERE id=?'.format(
                self.link_table, lcol, table), (existing_item, item_id, item_id))
        return affected_books, new_id

class ManyToManyTable(ManyToOneTable):

    '''
    Represents data that has a many-to-many mapping with books. i.e. each book
    can have more than one value and each value can be mapped to more than one
    book. For example: tags or authors.
    '''

    table_type = MANY_MANY
    selectq = 'SELECT book, {0} FROM {1} ORDER BY id'
    do_clean_on_remove = True

    def read_maps(self, db):
        bcm = defaultdict(list)
        cbm = self.col_book_map
        for book, item_id in db.conn.execute(
                self.selectq.format(self.metadata['link_column'], self.link_table)):
            cbm[item_id].add(book)
            bcm[book].append(item_id)

        self.book_col_map = {k:tuple(v) for k, v in bcm.iteritems()}

    def fix_link_table(self, db):
        linked_item_ids = {item_id for item_ids in self.book_col_map.itervalues() for item_id in item_ids}
        extra_item_ids = linked_item_ids - set(self.id_map)
        if extra_item_ids:
            for item_id in extra_item_ids:
                book_ids = self.col_book_map.pop(item_id, ())
                for book_id in book_ids:
                    self.book_col_map[book_id] = tuple(iid for iid in self.book_col_map.pop(book_id, ()) if iid not in extra_item_ids)
            db.conn.executemany('DELETE FROM {0} WHERE {1}=?'.format(
                self.link_table, self.metadata['link_column']), tuple((x,) for x in extra_item_ids))

    def remove_books(self, book_ids, db):
        clean = set()
        for book_id in book_ids:
            item_ids = self.book_col_map.pop(book_id, ())
            for item_id in item_ids:
                try:
                    self.col_book_map[item_id].discard(book_id)
                except KeyError:
                    if self.id_map.pop(item_id, null) is not null:
                        clean.add(item_id)
                else:
                    if not self.col_book_map[item_id]:
                        del self.col_book_map[item_id]
                        if self.id_map.pop(item_id, null) is not null:
                            clean.add(item_id)
        if clean and self.do_clean_on_remove:
            db.conn.executemany(
                'DELETE FROM {0} WHERE id=?'.format(self.metadata['table']),
                [(x,) for x in clean])
        return clean

    def remove_items(self, item_ids, db):
        affected_books = set()
        for item_id in item_ids:
            val = self.id_map.pop(item_id, null)
            if val is null:
                continue
            book_ids = self.col_book_map.pop(item_id, set())
            for book_id in book_ids:
                self.book_col_map[book_id] = tuple(x for x in self.book_col_map.get(book_id, ()) if x != item_id)
            affected_books.update(book_ids)
        item_ids = tuple((x,) for x in item_ids)
        db.conn.executemany('DELETE FROM {0} WHERE {1}=?'.format(self.link_table, self.metadata['link_column']), item_ids)
        db.conn.executemany('DELETE FROM {0} WHERE id=?'.format(self.metadata['table']), item_ids)
        return affected_books

    def rename_item(self, item_id, new_name, db):
        rmap = {icu_lower(v):k for k, v in self.id_map.iteritems()}
        existing_item = rmap.get(icu_lower(new_name), None)
        table, col, lcol = self.metadata['table'], self.metadata['column'], self.metadata['link_column']
        affected_books = self.col_book_map.get(item_id, set())
        new_id = item_id
        if existing_item is None or existing_item == item_id:
            # A simple rename will do the trick
            self.id_map[item_id] = new_name
            db.conn.execute('UPDATE {0} SET {1}=? WHERE id=?'.format(table, col), (new_name, item_id))
        else:
            # We have to replace
            new_id = existing_item
            self.id_map.pop(item_id, None)
            books = self.col_book_map.pop(item_id, set())
            # Replacing item_id with existing_item could cause the same id to
            # appear twice in the book list. Handle that by removing existing
            # item from the book list before replacing.
            for book_id in books:
                self.book_col_map[book_id] = tuple((existing_item if x == item_id else x) for x in self.book_col_map.get(book_id, ()) if x != existing_item)
            self.col_book_map[existing_item].update(books)
            db.conn.executemany('DELETE FROM {0} WHERE book=? AND {1}=?'.format(self.link_table, lcol), [
                (book_id, existing_item) for book_id in books])
            db.conn.execute('UPDATE {0} SET {1}=? WHERE {1}=?; DELETE FROM {2} WHERE id=?'.format(
                self.link_table, lcol, table), (existing_item, item_id, item_id))
        return affected_books, new_id


class AuthorsTable(ManyToManyTable):

    def read_id_maps(self, db):
        self.alink_map = lm = {}
        self.asort_map = sm = {}
        self.id_map = im = {}
        us = self.unserialize
        for aid, name, sort, link in db.conn.execute(
                'SELECT id, name, sort, link FROM authors'):
            name = us(name)
            im[aid] = name
            sm[aid] = (sort or author_to_author_sort(name))
            lm[aid] = link

    def set_sort_names(self, aus_map, db):
        aus_map = {aid:(a or '').strip() for aid, a in aus_map.iteritems()}
        aus_map = {aid:a for aid, a in aus_map.iteritems() if a != self.asort_map.get(aid, None)}
        self.asort_map.update(aus_map)
        db.conn.executemany('UPDATE authors SET sort=? WHERE id=?',
            [(v, k) for k, v in aus_map.iteritems()])
        return aus_map

    def set_links(self, link_map, db):
        link_map = {aid:(l or '').strip() for aid, l in link_map.iteritems()}
        link_map = {aid:l for aid, l in link_map.iteritems() if l != self.alink_map.get(aid, None)}
        self.alink_map.update(link_map)
        db.conn.executemany('UPDATE authors SET link=? WHERE id=?',
            [(v, k) for k, v in link_map.iteritems()])
        return link_map

    def remove_books(self, book_ids, db):
        clean = ManyToManyTable.remove_books(self, book_ids, db)
        for item_id in clean:
            self.alink_map.pop(item_id, None)
            self.asort_map.pop(item_id, None)
        return clean

    def rename_item(self, item_id, new_name, db):
        ret = ManyToManyTable.rename_item(self, item_id, new_name, db)
        if item_id not in self.id_map:
            self.alink_map.pop(item_id, None)
            self.asort_map.pop(item_id, None)
        else:
            # Was a simple rename, update the author sort value
            self.set_sort_names({item_id:author_to_author_sort(new_name)}, db)

        return ret

    def remove_items(self, item_ids, db):
        raise ValueError('Direct removal of authors is not allowed')

class FormatsTable(ManyToManyTable):

    do_clean_on_remove = False

    def read_id_maps(self, db):
        pass

    def read_maps(self, db):
        self.fname_map = fnm = defaultdict(dict)
        self.size_map = sm = defaultdict(dict)
        self.col_book_map = cbm = defaultdict(set)
        bcm = defaultdict(list)

        for book, fmt, name, sz in db.conn.execute('SELECT book, format, name, uncompressed_size FROM data'):
            if fmt is not None:
                fmt = fmt.upper()
                cbm[fmt].add(book)
                bcm[book].append(fmt)
                fnm[book][fmt] = name
                sm[book][fmt] = sz

        self.book_col_map = {k:tuple(sorted(v)) for k, v in bcm.iteritems()}

    def remove_books(self, book_ids, db):
        clean = ManyToManyTable.remove_books(self, book_ids, db)
        for book_id in book_ids:
            self.fname_map.pop(book_id, None)
            self.size_map.pop(book_id, None)
        return clean

    def set_fname(self, book_id, fmt, fname, db):
        self.fname_map[book_id][fmt] = fname
        db.conn.execute('UPDATE data SET name=? WHERE book=? AND format=?',
                        (fname, book_id, fmt))

    def remove_formats(self, formats_map, db):
        for book_id, fmts in formats_map.iteritems():
            self.book_col_map[book_id] = [fmt for fmt in self.book_col_map.get(book_id, []) if fmt not in fmts]
            for m in (self.fname_map, self.size_map):
                m[book_id] = {k:v for k, v in m[book_id].iteritems() if k not in fmts}
            for fmt in fmts:
                try:
                    self.col_book_map[fmt].discard(book_id)
                except KeyError:
                    pass
        db.conn.executemany('DELETE FROM data WHERE book=? AND format=?',
            [(book_id, fmt) for book_id, fmts in formats_map.iteritems() for fmt in fmts])
        def zero_max(book_id):
            try:
                return max(self.size_map[book_id].itervalues())
            except ValueError:
                return 0

        return {book_id:zero_max(book_id) for book_id in formats_map}

    def remove_items(self, item_ids, db):
        raise NotImplementedError('Cannot delete a format directly')

    def rename_item(self, item_id, new_name, db):
        raise NotImplementedError('Cannot rename formats')

    def update_fmt(self, book_id, fmt, fname, size, db):
        fmts = list(self.book_col_map.get(book_id, []))
        try:
            fmts.remove(fmt)
        except ValueError:
            pass
        fmts.append(fmt)
        self.book_col_map[book_id] = tuple(fmts)

        try:
            self.col_book_map[fmt].add(book_id)
        except KeyError:
            self.col_book_map[fmt] = {book_id}

        self.fname_map[book_id][fmt] = fname
        self.size_map[book_id][fmt] = size
        db.conn.execute('INSERT OR REPLACE INTO data (book,format,uncompressed_size,name) VALUES (?,?,?,?)',
                        (book_id, fmt, size, fname))
        return max(self.size_map[book_id].itervalues())

class IdentifiersTable(ManyToManyTable):

    def read_id_maps(self, db):
        pass

    def read_maps(self, db):
        self.book_col_map = defaultdict(dict)
        self.col_book_map = defaultdict(set)
        for book, typ, val in db.conn.execute('SELECT book, type, val FROM identifiers'):
            if typ is not None and val is not None:
                self.col_book_map[typ].add(book)
                self.book_col_map[book][typ] = val

    def remove_books(self, book_ids, db):
        clean = set()
        for book_id in book_ids:
            item_map = self.book_col_map.pop(book_id, {})
            for item_id in item_map:
                try:
                    self.col_book_map[item_id].discard(book_id)
                except KeyError:
                    clean.add(item_id)
                else:
                    if not self.col_book_map[item_id]:
                        del self.col_book_map[item_id]
                        clean.add(item_id)
        return clean

    def remove_items(self, item_ids, db):
        raise NotImplementedError('Direct deletion of identifiers is not implemented')

    def rename_item(self, item_id, new_name, db):
        raise NotImplementedError('Cannot rename identifiers')

    def all_identifier_types(self):
        return frozenset(k for k, v in self.col_book_map.iteritems() if v)

