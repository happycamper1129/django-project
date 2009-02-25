from pysolr import Solr
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.encoding import force_unicode
from haystack.backends import BaseSearchBackend, BaseSearchQuery
from haystack.models import SearchResult


# Word reserved by Solr for special use.
RESERVED_WORDS = (
    'AND',
    'NOT',
    'OR',
    'TO',
)

# Characters reserved by Solr for special use.
# The '\\' must come first, so as not to overwrite the other slash replacements.
RESERVED_CHARACTERS = (
    '\\', '+', '-', '&&', '||', '!', '(', ')', '{', '}', 
    '[', ']', '^', '"', '~', '*', '?', ':',
)


# TODO: Support for using Solr dynnamicField declarations, the magic fieldname
# postfixes like _i for integers. Requires some sort of global field registry
# though. Is it even worth it?


class SearchBackend(BaseSearchBackend):
    def __init__(self):
        if not hasattr(settings, 'SOLR_URL'):
            raise ImproperlyConfigured('You must specify a SOLR_URL in your settings.')
        
        # DRL_TODO: This should handle the connection more graceful, especially
        #           if the backend is down.
        self.conn = Solr(settings.SOLR_URL)

    def update(self, index, iterable, commit=True):
        docs = []
        
        try:
            for obj in iterable:
                doc = {}
                doc['id'] = self.get_identifier(obj)
                doc['django_ct_s'] = "%s.%s" % (obj._meta.app_label, obj._meta.module_name)
                doc['django_id_s'] = force_unicode(obj.pk)
                doc.update(index.prepare(obj))
                docs.append(doc)
        except UnicodeDecodeError:
            print "Chunk failed."
            pass
        
        self.conn.add(docs, commit=commit)

    def remove(self, obj, commit=True):
        solr_id = self.get_identifier(obj)
        self.conn.delete(id=solr_id, commit=commit)

    def clear(self, models=[], commit=True):
        if not models:
            # *:* matches all docs in Solr
            self.conn.delete(q='*:*', commit=commit)
        else:
            models_to_delete = []
            
            for model in models:
                models_to_delete.append("django_ct_s:%s.%s" % (model._meta.app_label, model._meta.module_name))
            
            self.conn.delete(q=" OR ".join(models_to_delete), commit=commit)
        
        # Run an optimize post-clear. http://wiki.apache.org/solr/FAQ#head-9aafb5d8dff5308e8ea4fcf4b71f19f029c4bb99
        self.conn.optimize()

    def search(self, query_string, sort_by=None, start_offset=0, end_offset=None,
               fields='', highlight=False, facets=None, date_facets=None, query_facets=None,
               existing_facets=None):
        if len(query_string) == 0:
            return []
        
        kwargs = {
            'fl': '* score',
        }
        
        if fields:
            kwargs['fl'] = fields
        
        if sort_by is not None:
            kwargs['sort'] = sort_by
        
        if start_offset is not None:
            kwargs['start'] = start_offset
        
        if end_offset is not None:
            kwargs['rows'] = end_offset
        
        if highlight is True:
            kwargs['hl'] = 'true'
            kwargs['hl.fragsize'] = '200'
        
        if facets is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.field'] = facets
        
        if date_facets is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.date'] = date_facets.keys()
            
            for key, value in date_facets.items():
                # Date-based facets in Solr kinda suck.
                kwargs["f.%s.facet.date.start" % key] = self.conn._from_python(value.get('start_date'))
                kwargs["f.%s.facet.date.end" % key] = self.conn._from_python(value.get('end_date'))
                kwargs["f.%s.facet.date.gap" % key] = value.get('gap')
        
        if query_facets is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.query'] = ["%s:%s" % (field, value) for field, value in query_facets.items()]
        
        if existing_facets is not None:
            kwargs['facet'] = 'on'
            kwargs['fq'] = ["%s:%s" % (field, value) for field, value in existing_facets.items()]
        
        raw_results = self.conn.search(query_string, **kwargs)
        return self._process_results(raw_results, highlight=highlight)
    
    def more_like_this(self, model_instance):
        from haystack.sites import site, NotRegistered
        index = site.get_index(model_instance.__class__)
        field_name = index.get_content_field()    
        raw_results = self.conn.more_like_this("id:%s" % self.get_identifier(model_instance), field_name, fl='*,score')
        return self._process_results(raw_results)
    
    def _process_results(self, raw_results, highlight=False):
        results = []
        facets = {}
        
        if hasattr(raw_results, 'facets'):
            facets = {
                'fields': raw_results.facets.get('facet_fields', {}),
                'dates': raw_results.facets.get('facet_dates', {}),
                'queries': raw_results.facets.get('facet_queries', {}),
            }
            
            for key in ['fields']:
                for facet_field in facets[key]:
                    # Convert to a dict, as Solr's json format returns a list of
                    # pairs.
                    facets[key][facet_field] = dict(zip(facets[key][facet_field][::2], facets[key][facet_field][1::2]))
        
        for raw_result in raw_results.docs:
            app_label, model_name = raw_result['django_ct_s'].split('.')
            additional_fields = {}
            
            for key, value in raw_result.items():
                additional_fields[str(key)] = value
            
            del(additional_fields['django_ct_s'])
            del(additional_fields['django_id_s'])
            del(additional_fields['score'])
            
            if raw_result['id'] in getattr(raw_results, 'highlighting', {}):
                additional_fields['highlighted'] = raw_results.highlighting[raw_result['id']]
            
            result = SearchResult(app_label, model_name, raw_result['django_id_s'], raw_result['score'], **additional_fields)
            results.append(result)
        
        return {
            'results': results,
            'hits': raw_results.hits,
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
            query = '*:*'
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
                        'gt': "%s:{%s TO *}",
                        'gte': "%s:[%s TO *]",
                        'lt': "%s:{* TO %s}",
                        'lte': "%s:[* TO %s]",
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
            order_by_list = []
            
            for order_by in self.order_by:
                if order_by.startswith('-'):
                    order_by_list.append('%s desc' % order_by[1:])
                else:
                    order_by_list.append('%s asc' % order_by)
            
            kwargs['sort_by'] = ", ".join(order_by_list)
        
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
        
        if self.existing_facets:
            kwargs['existing_facets'] = self.existing_facets
        
        results = self.backend.search(final_query, **kwargs)
        self._results = results.get('results', [])
        self._hit_count = results.get('hits', 0)
