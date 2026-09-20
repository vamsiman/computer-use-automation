// The WebForms postback shim.
//
// Navigation controls in this app are anchors whose href is a javascript:
// call rather than a URL. That is not decoration: it means the automation
// cannot shortcut by reading an href and navigating to it, and has to locate
// the control and genuinely click it, which is the behaviour we want to
// exercise on a surface with no clean DOM.
function __doPostBack(eventTarget, eventArgument) {
    switch (eventTarget) {
        case 'ctl00$ContentPlaceHolder1$lnkNewSub':
            window.location.href =
                '/members/' + eventArgument + '/subaccount/new';
            break;
        case 'ctl00$ContentPlaceHolder1$lnkBackToMember':
            window.location.href = '/members/' + eventArgument;
            break;
        case 'ctl00$ContentPlaceHolder1$lnkBackToSearch':
            window.location.href = '/members/search';
            break;
        default:
            break;
    }
}
