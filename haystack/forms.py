from django import forms
from django.db import models
import haystack
from haystack.query import SearchQuerySet


def model_choices(site=None):
    if site is None:
        site = haystack.sites.site
    
    choices = [(m._meta, unicode(m._meta.verbose_name_plural)) for m in site.get_indexed_models()]
    return sorted(choices, key=lambda x: x[1])


class SearchForm(forms.Form):
    query = forms.CharField(required=False)
    
    def __init__(self, *args, **kwargs):
        self.searchqueryset = kwargs.get('searchqueryset', None)
        
        if self.searchqueryset is None:
            self.searchqueryset = SearchQuerySet()
        
        try:
            del(kwargs['searchqueryset'])
        except KeyError:
            pass
        
        super(SearchForm, self).__init__(*args, **kwargs)
    
    def search(self):
        self.clean()
        return self.searchqueryset.auto_query(self.cleaned_data['query'])


class HighlightedSearchForm(SearchForm):
    def search(self):
        return super(HighlightedSearchForm, self).search().highlight()


class ModelSearchForm(SearchForm):
    def __init__(self, *args, **kwargs):
        super(ModelSearchForm, self).__init__(*args, **kwargs)
        self.fields['models'] = forms.MultipleChoiceField(choices=model_choices(), required=False, widget=forms.CheckboxSelectMultiple)

    def get_models(self):
        """Return an alphabetical list of model classes in the index."""
        search_models = []
        
        for model in self.cleaned_data['models']:
            search_models.append(models.get_model(*model.split('.')))
        
        return search_models
    
    def search(self):
        sqs = super(ModelSearchForm, self).search()
        return sqs.models(self.get_models())


class HighlightedModelSearchForm(ModelSearchForm):
    def search(self):
        return super(HighlightedModelSearchForm, self).search().highlight()
