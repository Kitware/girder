import FlowViewTemplate from '../templates/flowView.pug';

import '../stylesheets/flowView.styl';

const $ = girder.$;
const View = girder.views.View;
const { confirm } = girder.dialog;
const events = girder.events;
const FileModel = girder.models.FileModel;
const { AccessType } = girder.constants;

var FlowView = View.extend({
    events: {
        'click .g-thumbnail-delete': function (e) {
            var container = $(e.currentTarget).parents('.g-thumbnail-container');
            var file = new FileModel({ _id: container.attr('g-file-id') });

            confirm({
                text: 'Are you sure you want to delete this thumbnail?',
                yesText: 'Delete',
                confirmCallback: () => {
                    file.on('g:deleted', function () {
                        container.remove();
                    }).on('g:error', function () {
                        events.trigger('g:alert', {
                            icon: 'cancel',
                            text: 'Failed to delete thumbnail.',
                            type: 'danger',
                            timeout: 4000
                        });
                    }).destroy();
                }
            });
        },

        'mouseenter .g-thumbnail-container': function () {
            this.$('.g-thumbnail-actions-container').addClass('g-show');
        },

        'mouseleave .g-thumbnail-container': function () {
            this.$('.g-thumbnail-actions-container').removeClass('g-show');
        }
    },

    initialize: function (settings) {
        this.thumbnails = settings.thumbnails;
        this.accessLevel = settings.accessLevel || AccessType.READ;
    },

    render: function () {
        this.$el.html(FlowViewTemplate({
            thumbnails: this.thumbnails.toArray(),
            accessLevel: this.accessLevel,
            AccessType: AccessType
        }));

        return this;
    }
});

export default FlowView;
