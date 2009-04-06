import os
from whoosh import store
from whoosh.fields import Schema, ID, STORED, TEXT, KEYWORD
import whoosh.index as index
from whoosh.qparser import QueryParser
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.encoding import force_unicode
from haystack.backends import BaseSearchBackend, BaseSearchQuery, SearchBackendError
from haystack.models import SearchResult


# Word reserved by Whoosh for special use.
RESERVED_WORDS = (
    'AND',
    'NOT',
    'OR',
    'TO',
)

# Characters reserved by Whoosh for special use.
# The '\\' must come first, so as not to overwrite the other slash replacements.
RESERVED_CHARACTERS = (
    '\\', '+', '-', '&&', '||', '!', '(', ')', '{', '}', 
    '[', ']', '^', '"', '~', '*', '?', ':',
)


class SearchBackend(BaseSearchBackend):
    def __init__(self, site=None):
        super(SearchBackend, self).__init__(site)
        self.setup_complete = False
        
        if not hasattr(settings, 'WHOOSH_PATH'):
            raise ImproperlyConfigured('You must specify a WHOOSH_PATH in your settings.')
    
    def setup(self):
        """
        Defers loading until needed.
        """
        # DRL_FIXME: This is a workaround to the current SearchSite loading
        #            issues. Once that is fixed, this may no longer be
        #            necessary.
        new_index = False
        
        # Make sure the index is there.
        if not os.path.exists(settings.WHOOSH_PATH):
            os.makedirs(settings.WHOOSH_PATH)
            new_index = True
        
        self.storage = store.FileStorage(settings.WHOOSH_PATH)
        self.content_field_name, fields = self.site.build_unified_schema()
        self.schema = self.build_schema(fields)
        self.parser = QueryParser(self.content_field_name, schema=self.schema)
        
        if new_index is True:
            self.index = index.create_in(settings.WHOOSH_PATH, self.schema)
        else:
            try:
                self.index = index.Index(self.storage, schema=self.schema)
            except index.EmptyIndexError:
                self.index = index.create_in(settings.WHOOSH_PATH, self.schema)
        
        self.setup_complete = True
    
    def build_schema(self, fields):
        schema_fields = {
            'id': ID(stored=True, unique=True),
            'django_ct_s': ID(stored=True),
            'django_id_s': ID(stored=True),
        }
        
        for field in fields:
            if field['multi_valued'] is True:
                schema_fields[field['field_name']] = KEYWORD(stored=True, comma=True)
            elif field['type'] in ('slong', 'sfloat', 'boolean', 'date'):
                if field['indexed'] is False:
                    schema_fields[field['field_name']] = STORED
                else:
                    schema_fields[field['field_name']] = ID(stored=True)
            elif field['type'] == 'text':
                schema_fields[field['field_name']] = TEXT(stored=True)
            else:
                raise SearchBackendError("Whoosh backend does not support type '%s'. Please report this bug." % field['type'])
        
        return Schema(**schema_fields)

    def update(self, index, iterable, commit=True):
        if not self.setup_complete:
            self.setup()
        
        # DRL_TODO: Perhaps add locking here?
        # self.index.lock()
        # try:
        writer = self.index.writer()
        
        for obj in iterable:
            doc = {}
            doc['id'] = force_unicode(self.get_identifier(obj))
            doc['django_ct_s'] = force_unicode("%s.%s" % (obj._meta.app_label, obj._meta.module_name))
            doc['django_id_s'] = force_unicode(obj.pk)
            other_data = index.prepare(obj)
            
            # Really make sure it's unicode, because Whoosh won't have it any
            # other way.
            for key in other_data:
                other_data[key] = force_unicode(other_data[key])
            
            doc.update(other_data)
            writer.update_document(**doc)
        
        # finally:
        #    self.index.unlock()
        
        if commit is True:
            writer.commit()

    def remove(self, obj, commit=True):
        if not self.setup_complete:
            self.setup()
        
        whoosh_id = self.get_identifier(obj)
        self.index.delete_by_query(q=self.parser.parse('id:"%s"' % whoosh_id))
        
        if commit is True:
            self.index.commit()

    def clear(self, models=[], commit=True):
        if not self.setup_complete:
            self.setup()
        
        if not models:
            # *:* matches all docs in Whoosh
            self.index.delete_by_query(q=self.parser.parse('*'))
        else:
            models_to_delete = []
            
            for model in models:
                models_to_delete.append("django_ct_s:%s.%s" % (model._meta.app_label, model._meta.module_name))
            
            self.index.delete_by_query(q=self.parser.parse(" OR ".join(models_to_delete)))
        
        if commit is True:
            self.index.commit()
        
    def optimize(self):
        if not self.setup_complete:
            self.setup()
        
        self.index.optimize()

    def search(self, query_string, sort_by=None, start_offset=0, end_offset=None,
               fields='', highlight=False, facets=None, date_facets=None, query_facets=None,
               narrow_queries=None):
        if not self.setup_complete:
            self.setup()
        
        if len(query_string) == 0:
            return []
        
        reverse = False
        
        if sort_by is not None:
            # Determine if we need to reverse the results and if Whoosh can
            # handle what it's being asked to sort by. Reversing is an
            # all-or-nothing action, unfortunately.
            for order_by in sort_by:
                if order_by.startswith('-'):
                    if len(sort_by) > 1:
                        raise SearchBackendError("Whoosh does not handle more than one field being ordered in reverse.")
                    
                    reverse = True
        
        if facets is not None:
            # raise SearchBackendError("Whoosh does not handle faceting.")
            pass
        
        if date_facets is not None:
            # raise SearchBackendError("Whoosh does not handle date faceting.")
            pass
        
        if query_facets is not None:
            # raise SearchBackendError("Whoosh does not handle query faceting.")
            pass
        
        if narrow_queries is not None:
            # DRL_FIXME: Determine if Whoosh can do this.
            # kwargs['fq'] = list(narrow_queries)
            pass
        
        if self.index.doc_count:
            searcher = self.index.searcher()
            # DRL_TODO: Ignoring offsets for now, as slicing caused issues with pagination.
            raw_results = searcher.search(self.parser.parse(query_string), sortedby=sort_by, reverse=reverse)
            return self._process_results(raw_results, highlight=highlight, query_string=query_string)
        else:
            return {
                'results': [],
                'hits': 0,
            }
    
    def more_like_this(self, model_instance):
        # raise SearchBackendError("Whoosh does not handle More Like This.")
        return {
            'results': [],
            'hits': 0,
        }
    
    def _process_results(self, raw_results, highlight=False, query_string=''):
        results = []
        facets = {}
        
        for raw_result in raw_results:
            raw_result = dict(raw_result)
            app_label, model_name = raw_result['django_ct_s'].split('.')
            additional_fields = {}
            
            for key, value in raw_result.items():
                additional_fields[str(key)] = value
            
            del(additional_fields['django_ct_s'])
            del(additional_fields['django_id_s'])
            # DRL_FIXME: Figure out if there's a way to get the score out of Whoosh.
            # del(additional_fields['score'])
            
            if highlight:
                from whoosh import analysis
                from whoosh.highlight import highlight, ContextFragmenter, UppercaseFormatter
                sa = analysis.StemmingAnalyzer()
                terms = [term.replace('*', '') for term in query_string.split()]
                
                # DRL_FIXME: Highlighting doesn't seem to work properly in testing.
                additional_fields['highlighted'] = {
                    self.content_field_name: [highlight(additional_fields.get(self.content_field_name), terms, sa, ContextFragmenter(terms), UppercaseFormatter())],
                }
            
            result = SearchResult(app_label, model_name, raw_result['django_id_s'], raw_result.get('score', 0), **additional_fields)
            results.append(result)
        
        return {
            'results': results,
            'hits': len(results),
            'facets': facets,
        }


class SearchQuery(BaseSearchQuery):
    def __init__(self, backend=None):
        super(SearchQuery, self).__init__(backend=backend)
        self.backend = backend or SearchBackend()
    
    def build_query(self):
        query = ''
        
        if not self.query_filters:
            # Match all.
            query = '*'
        else:
            query_chunks = []
            
            for the_filter in self.query_filters:
                if the_filter.is_and():
                    query_chunks.append('AND')
                
                if the_filter.is_not():
                    query_chunks.append('NOT')
                
                if the_filter.is_or():
                    query_chunks.append('OR')
                
                value = the_filter.value
                
                if isinstance(value, (int, long, float, complex)):
                    value = str(value)
                
                # Check to see if it's a phrase for an exact match.
                if ' ' in value:
                    value = '"%s"' % value
                
                # 'content' is a special reserved word, much like 'pk' in
                # Django's ORM layer. It indicates 'no special field'.
                if the_filter.field == 'content':
                    query_chunks.append(value)
                else:
                    filter_types = {
                        'exact': "%s:%s",
                        'gt': "%s:%s..*",
                        'gte': "NOT %s:*..%s",
                        'lt': "%s:*..%s",
                        'lte': "NOT %s:%s..*",
                    }
                    
                    if the_filter.filter_type != 'in':
                        query_chunks.append(filter_types[the_filter.filter_type] % (the_filter.field, value))
                    else:
                        in_options = []
                        
                        for possible_value in value:
                            in_options.append("%s:%s" % (the_filter.field, possible_value))
                        
                        query_chunks.append("(%s)" % " OR ".join(in_options))
            
            if query_chunks[0] in ('AND', 'OR'):
                # Pull off an undesirable leading "AND" or "OR".
                del(query_chunks[0])
            
            query = " ".join(query_chunks)
        
        if len(self.models):
            models = ['django_ct_s:"%s.%s"' % (model._meta.app_label, model._meta.module_name) for model in self.models]
            models_clause = ' OR '.join(models)
            final_query = '(%s) AND (%s)' % (query, models_clause)
        else:
            final_query = query
        
        if self.boost:
            boost_list = []
            
            for boost_word, boost_value in self.boost.items():
                boost_list.append("%s^%s" % (boost_word, boost_value))
            
            final_query = "%s %s" % (final_query, " ".join(boost_list))
        
        return final_query
    
    def clean(self, query_fragment):
        """Sanitizes a fragment from using reserved character/words."""
        words = query_fragment.split()
        cleaned_words = []
        
        for word in words:
            if word in RESERVED_WORDS:
                word = word.replace(word, word.lower())
        
            for char in RESERVED_CHARACTERS:
                word = word.replace(char, '\\%s' % char)
            
            cleaned_words.append(word)
        
        return " ".join(cleaned_words)
    
    def run(self):
        """Builds and executes the query. Returns a list of search results."""
        final_query = self.build_query()
        kwargs = {
            'start_offset': self.start_offset,
        }
        
        if self.order_by:
            kwargs['sort_by'] = self.order_by
        
        if self.end_offset is not None:
            kwargs['end_offset'] = self.end_offset - self.start_offset
        
        if self.highlight:
            kwargs['highlight'] = self.highlight
        
        if self.facets:
            kwargs['facets'] = list(self.facets)
        
        if self.date_facets:
            kwargs['date_facets'] = self.date_facets
        
        if self.query_facets:
            kwargs['query_facets'] = self.query_facets
        
        if self.narrow_queries:
            kwargs['narrow_queries'] = self.narrow_queries
        
        results = self.backend.search(final_query, **kwargs)
        self._results = results.get('results', [])
        self._hit_count = results.get('hits', 0)
        self._facet_counts = results.get('facets', {})
