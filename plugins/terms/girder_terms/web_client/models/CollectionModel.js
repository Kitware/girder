const { getCurrentUser } = girder.auth;
const { restRequest } = girder.rest;
const CollectionModel = girder.models.CollectionModel;

const termsAcceptedFallback = {};

CollectionModel.prototype.hasTerms = function () {
    // An empty string also means there are no terms.
    return Boolean(this.get('terms'));
};

CollectionModel.prototype.currentUserHasAcceptedTerms = async function () {
    const termsHash = await this._hashTerms();
    const currentUser = getCurrentUser();
    if (currentUser) {
        const userAcceptedTerms = currentUser.get('terms');
        // Lodash's _.get would be nice here
        return userAcceptedTerms?.collection?.[this.id]?.hash === termsHash;
    } else {
        const storageKey = `terms.collection.${this.id}`;
        try {
            return window.localStorage.getItem(storageKey) === termsHash;
        } catch (e) {
            return termsAcceptedFallback[this.id] === termsHash;
        }
    }
};

CollectionModel.prototype.currentUserSetAcceptTerms = async function () {
    const termsHash = await this._hashTerms();
    const currentUser = getCurrentUser();
    if (currentUser) {
        return restRequest({
            url: `collection/${this.id}/acceptTerms`,
            method: 'POST',
            data: {
                termsHash: termsHash
            }
        })
            .done(() => {
                // Even if this endpoint returned an updated copy of the user document, it wouldn't
                // be safe to just "setCurrentUser" with that document here, since the login method
                // performs some special transformations (e.g. setting a "token" attribute) before
                // instantiating a new UserModel, and it would be too fragile to reproduce those
                // here. We also don't want to trigger a brand-new login. So, just update the
                // currentUser's "terms" attribute in-place, triggering a "change" event.
                const userAcceptedTerms = currentUser.get('terms') || {};
                // This code would be much cleaner with _.merge from Lodash.
                userAcceptedTerms.collection = userAcceptedTerms.collection || {};
                userAcceptedTerms.collection[this.id] = {
                    hash: termsHash,
                    // 'accepted' is from a server-set timestamp, so we don't know it here. However,
                    // its value is irrelevant, as it's for auditing purposes only.
                    accepted: null
                };
                currentUser.set('terms', userAcceptedTerms);
            });
    } else {
        const storageKey = `terms.collection.${this.id}`;
        try {
            window.localStorage.setItem(storageKey, termsHash);
        } catch (e) {
            termsAcceptedFallback[this.id] = termsHash;
        }
        return girder.$.Deferred().resolve().promise();
    }
};

CollectionModel.prototype._hashTerms = async function () {
    const arr = new window.TextEncoder().encode(this.get('terms'));
    const hashBuffer = await window.crypto.subtle.digest('SHA-256', arr);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('');
};
